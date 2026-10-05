from __future__ import annotations

from typing import Literal

import pytest
from pydantic import BaseModel

from graph_engine.artifacts import ArtifactRef
from graph_engine.attempts.resolutions import PermanentTaskFailure
from graph_engine.flow import Flow, FlowCheckError
from graph_engine.stategraph.ledger import NamedWrite, ledger_refs
from graph_engine.testing import committed

from support import RECEIPT, ChangeInput, MarkerOutput, Toy, calls_of, contract, open_harness, state_of


class _Selected(BaseModel):
    change_id: str = "c1"
    selected: list[str] = []


def _lane(name: str, root: str):
    task = contract(name)
    flow = Flow(name, input=ChangeInput, outcomes=("passed", "failed"))
    flow.step(
        "codegen",
        Toy(task, namespace="generation", writes=(NamedWrite(name=name, root=root),)),
        on_failure="failed",
        then="passed",
    )
    return task, flow


def _generation():
    api_task, api = _lane("api", "qa/api.md")
    e2e_task, e2e = _lane("e2e", "qa/e2e.md")
    flow = Flow("generation", input=_Selected, outcomes=("passed", "failed"))
    flow.parallel(
        "families",
        branches={"api": api, "e2e": e2e},
        select="selected",
        require="passed",
        then="passed",
        on_failure="failed",
    )
    harness, context = open_harness(api_task, e2e_task)
    return harness, flow.compile(context)


def _ref(path: str, digest: str) -> ArtifactRef:
    return ArtifactRef(path=path, digest=digest)


async def test_parallel_runs_the_selected_branches_and_marks_the_rest_skipped() -> None:
    harness, graph = _generation()
    result = await harness.run(
        graph,
        input={"change_id": "c1", "selected": ["api"]},
        script={
            "generation.api.codegen": [
                committed(MarkerOutput(), RECEIPT, artifacts=(_ref("qa/api.md", "a" * 64),))
            ],
            "generation.e2e.codegen": [
                committed(MarkerOutput(), RECEIPT, artifacts=(_ref("qa/e2e.md", "b" * 64),))
            ],
        },
    )

    finished = state_of(result)
    assert finished["flow_outcome"] == "passed"
    assert finished["flow_control"]["results"] == {"e2e": "skipped", "api": "passed"}
    assert calls_of(result) == ["generation.api.codegen"]
    assert ledger_refs(finished["artifact_ledger"], "generation.api")[0]["path"] == "qa/api.md"


async def test_parallel_on_failure_keeps_both_branch_failures_and_ledgers() -> None:
    harness, graph = _generation()
    failed = await harness.run(
        graph,
        input={"change_id": "c1", "selected": ["api", "e2e"]},
        script={
            "generation.api.codegen": [PermanentTaskFailure(kind="invalid_output", message="bad")],
            "generation.e2e.codegen": [PermanentTaskFailure(kind="invalid_output", message="bad")],
        },
    )
    finished = state_of(failed)
    assert finished["flow_outcome"] == "failed"
    assert finished["flow_control"]["results"] == {"api": "failed", "e2e": "failed"}
    assert set(finished["flow_control"]["failures"]) == {"api", "e2e"}
    assert calls_of(failed) == ["generation.api.codegen", "generation.e2e.codegen"]

    harness, graph = _generation()
    both = await harness.run(
        graph,
        input={"change_id": "c1", "selected": ["api", "e2e"]},
        script={
            "generation.api.codegen": [
                committed(MarkerOutput(), RECEIPT, artifacts=(_ref("qa/api.md", "a" * 64),))
            ],
            "generation.e2e.codegen": [
                committed(MarkerOutput(), RECEIPT, artifacts=(_ref("qa/e2e.md", "b" * 64),))
            ],
        },
    )
    ledger = state_of(both)["artifact_ledger"]
    assert ledger_refs(ledger, "generation.api")[0]["path"] == "qa/api.md"
    assert ledger_refs(ledger, "generation.e2e")[0]["path"] == "qa/e2e.md"
    assert ledger["generation.api"]["receipt"]["receipt_id"] == RECEIPT.receipt_id


@pytest.mark.parametrize("rewriter", ["a", "z"])
async def test_a_branch_rewrite_is_not_undone_by_a_sibling_copy_of_the_old_ledger(rewriter: str) -> None:
    seed = contract("seed")
    tasks = {key: contract(key) for key in ("a", "z")}
    rewritten = NamedWrite(name="shared", root="qa/shared.md")
    flow = Flow("generation", input=ChangeInput, outcomes=("passed", "failed"))
    flow.step(
        "seed",
        Toy(seed, namespace="generation", writes=(rewritten,)),
        on_failure="failed",
        then="families",
    )
    branches: dict[str, object] = {}
    for key, task in tasks.items():
        writes = (rewritten,) if key == rewriter else (NamedWrite(name=key, root=f"qa/{key}.md"),)
        branches[key] = Toy(task, namespace="generation", writes=writes)
    flow.parallel(
        "families",
        branches=branches,
        select=None,
        require="succeeded",
        then="passed",
        on_failure="failed",
    )
    harness, context = open_harness(seed, *tasks.values())
    other = "z" if rewriter == "a" else "a"
    result = await harness.run(
        flow.compile(context),
        input={},
        script={
            "generation.seed": [
                committed(MarkerOutput(), RECEIPT, artifacts=(_ref("qa/shared.md", "1" * 64),))
            ],
            f"generation.{rewriter}": [
                committed(MarkerOutput(), RECEIPT, artifacts=(_ref("qa/shared.md", "2" * 64),))
            ],
            f"generation.{other}": [
                committed(MarkerOutput(), RECEIPT, artifacts=(_ref(f"qa/{other}.md", "3" * 64),))
            ],
        },
    )

    ledger = state_of(result)["artifact_ledger"]
    assert ledger_refs(ledger, "generation.shared") == [{"path": "qa/shared.md", "digest": "2" * 64}]
    assert ledger["generation.shared"]["receipt"]["receipt_id"] == RECEIPT.receipt_id
    assert ledger_refs(ledger, f"generation.{other}")[0]["path"] == f"qa/{other}.md"


async def test_select_none_runs_every_branch_including_a_single_op() -> None:
    api = contract("api")
    e2e = contract("e2e")
    flow = Flow("generation", input=ChangeInput, outcomes=("passed", "failed"))
    flow.parallel(
        "families",
        branches={"api": api, "e2e": e2e},
        select=None,
        require="succeeded",
        then="passed",
        on_failure="failed",
    )
    harness, context = open_harness(api, e2e)
    result = await harness.run(
        flow.compile(context),
        input={},
        script={
            "lane.api": [committed(MarkerOutput(), RECEIPT)],
            "lane.e2e": [committed(MarkerOutput(), RECEIPT)],
        },
    )

    assert state_of(result)["flow_outcome"] == "passed"
    assert state_of(result)["flow_control"]["results"] == {"api": "succeeded", "e2e": "succeeded"}
    assert calls_of(result) == ["lane.api", "lane.e2e"]


async def test_an_empty_selection_is_vacuously_successful() -> None:
    api = contract("api")
    flow = Flow("generation", input=_Selected, outcomes=("passed", "failed"))
    flow.parallel(
        "families",
        branches={"api": api},
        select="selected",
        require="succeeded",
        then="passed",
        on_failure="failed",
    )
    harness, context = open_harness(api)
    result = await harness.run(flow.compile(context), input={"selected": []}, script={})
    assert state_of(result)["flow_outcome"] == "passed"
    assert state_of(result)["flow_control"]["results"] == {"api": "skipped"}


def test_parallel_rejects_overlapping_ledger_keys_and_unknown_literals() -> None:
    shared = NamedWrite(name="files", root="qa/tests")
    api_task = contract("api")
    e2e_task = contract("e2e")
    api = Flow("api", input=ChangeInput, outcomes=("passed", "failed"))
    api.step("codegen", Toy(api_task, writes=(shared,)), on_failure="failed", then="passed")
    e2e = Flow("e2e", input=ChangeInput, outcomes=("passed", "failed"))
    e2e.step("codegen", Toy(e2e_task, writes=(shared,)), on_failure="failed", then="passed")
    flow = Flow("generation", input=ChangeInput, outcomes=("passed", "failed"))
    flow.parallel(
        "families",
        branches={"api": api, "e2e": e2e},
        select=None,
        require="passed",
        then="passed",
        on_failure="failed",
    )
    _harness, context = open_harness(api_task, e2e_task)
    with pytest.raises(FlowCheckError, match="both write"):
        flow.compile(context)

    class _LiteralSelect(BaseModel):
        selected: list[Literal["api", "web"]]

    literal = Flow("generation", input=_LiteralSelect, outcomes=("passed", "failed"))
    literal.parallel(
        "families",
        branches={"api": api},
        select="selected",
        require="passed",
        then="passed",
        on_failure="failed",
    )
    with pytest.raises(FlowCheckError, match="are not branches"):
        literal.compile(context)
