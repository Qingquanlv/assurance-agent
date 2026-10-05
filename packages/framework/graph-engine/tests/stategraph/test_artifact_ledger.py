from __future__ import annotations

from typing import Annotated

import pytest
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, ValidationError

from graph_engine.artifacts import ArtifactRef
from graph_engine.stategraph.ledger import (
    AttemptLedgerState,
    InputBinding,
    NamedWrite,
    bind_input_slots,
    fill_artifact_ledger,
    ledger_refs,
    merge_artifact_ledger,
)
from graph_engine.stategraph.publish import bind_produced_artifacts

_CASE = NamedWrite(name="case", root="qa/cases", many=True)
_PROPOSAL = NamedWrite(name="proposal", root="qa/proposal.md")


class _BoundInput(BaseModel):
    name: str
    plan_ref: dict[str, str]
    case_refs: list[dict[str, str]] = []


def test_bind_input_slots_overlays_a_mapping_and_leaves_missing_slots_absent() -> None:
    plan = {"path": "qa/plan.json", "digest": "a" * 64}
    first = {"path": "qa/cases/menus/case.yaml", "digest": "b" * 64}
    second = {"path": "qa/cases/orders/case.yaml", "digest": "c" * 64}
    bindings = (
        InputBinding(ledger_key="intake.plan", field="plan_ref"),
        InputBinding(ledger_key="intake.case", field="case_refs", many=True),
    )

    def select_mapping(_state: object) -> dict[str, object]:
        return {"name": "demo"}

    wrapped = bind_input_slots(select_mapping, bindings)
    assert wrapped({"artifact_ledger": {"intake.plan": [plan], "intake.case": [second, first]}}) == {
        "name": "demo",
        "plan_ref": plan,
        "case_refs": [first, second],
    }
    missing = wrapped({"artifact_ledger": {}})
    assert missing == {"name": "demo"}
    with pytest.raises(ValidationError):
        _BoundInput.model_validate(missing)

    many = bind_input_slots(
        select_mapping,
        (InputBinding(ledger_key="intake.case", field="case_refs", many=True),),
    )
    assert many({"artifact_ledger": {"intake.case": [first]}})["case_refs"] == [first]
    single = bind_input_slots(
        select_mapping,
        (InputBinding(ledger_key="intake.plan", field="plan_ref"),),
    )
    assert "plan_ref" not in single({"artifact_ledger": {"intake.plan": [plan, first]}})


def test_bind_input_slots_keeps_the_model_path() -> None:
    plan = {"path": "qa/plan.json", "digest": "a" * 64}
    other = {"path": "qa/other.json", "digest": "b" * 64}
    original = _BoundInput(name="demo", plan_ref={"path": "stale", "digest": "d" * 64})

    def select_model(_state: object) -> _BoundInput:
        return original

    wrapped = bind_input_slots(
        select_model,
        (InputBinding(ledger_key="intake.plan", field="plan_ref"),),
    )
    assert wrapped({"artifact_ledger": {}}) is original
    assert wrapped({"artifact_ledger": {"intake.plan": [plan, other]}}) is original
    replaced = wrapped({"artifact_ledger": {"intake.plan": [plan]}})
    assert isinstance(replaced, _BoundInput)
    assert replaced is not original
    assert replaced.plan_ref == plan
    assert replaced.name == "demo"


def test_merge_artifact_ledger_lets_the_later_list_win() -> None:
    left = {"intake.case": [{"path": "qa/cases/menus/case.yaml", "digest": "a" * 64}]}
    right = {"intake.case": [{"path": "qa/cases/menus/case.yaml", "digest": "b" * 64}]}
    assert merge_artifact_ledger(None, left) == left
    assert merge_artifact_ledger(left, right)["intake.case"][0]["digest"] == "b" * 64
    assert merge_artifact_ledger(left, {}) == left
    bare = {"intake.plan": {"path": "qa/proposal.md", "digest": "c" * 64}}
    assert merge_artifact_ledger(None, bare)["intake.plan"] == [
        {"path": "qa/proposal.md", "digest": "c" * 64}
    ]


def test_fill_artifact_ledger_uses_committed_refs_for_a_directory() -> None:
    committed = (
        ArtifactRef(path="qa/cases/menus/case.yaml", digest="a" * 64),
        ArtifactRef(path="qa/proposal.md", digest="c" * 64),
        ArtifactRef(path="qa/cases/orders/case.yaml", digest="d" * 64),
    )
    ledger = fill_artifact_ledger("intake", (_CASE, _PROPOSAL), committed)
    assert ledger_refs(ledger, "intake.case") == [
        {"path": "qa/cases/menus/case.yaml", "digest": "a" * 64},
        {"path": "qa/cases/orders/case.yaml", "digest": "d" * 64},
    ]
    assert ledger["intake.proposal"] == [{"path": "qa/proposal.md", "digest": "c" * 64}]
    assert "intake.case:qa/cases/menus/case.yaml" not in ledger


def test_unnamed_committed_files_are_not_handed_off() -> None:
    committed = (ArtifactRef(path="qa/.qa.yaml", digest="a" * 64),)
    assert fill_artifact_ledger("intake", (_CASE,), committed) == {}


def test_bind_produced_artifacts_ignores_agent_claimed_paths_and_publish_ledger() -> None:
    def publish(state: dict[str, object], output: object, receipt: object, *, committed: object = ()) -> dict:
        del state, output, receipt, committed
        return {
            "status": "kept",
            "artifact_ledger": {"intake.case": [{"path": "qa/cases/other/case.yaml", "digest": "e" * 64}]},
        }

    bound = bind_produced_artifacts(publish, namespace="intake", writes=(_CASE,))
    update = bound(
        {},
        {"artifacts": [{"path": "qa/cases/other/case.yaml", "digest": "e" * 64}]},
        None,
        committed=(ArtifactRef(path="qa/cases/menus/case.yaml", digest="a" * 64),),
    )
    assert update["status"] == "kept"
    assert ledger_refs(update["artifact_ledger"], "intake.case") == [
        {"path": "qa/cases/menus/case.yaml", "digest": "a" * 64}
    ]


def test_attempt_ledger_state_reducer_merges_across_nodes() -> None:
    class State(AttemptLedgerState, total=False):
        artifact_ledger: Annotated[dict[str, list[dict[str, str]]], merge_artifact_ledger]

    def first(state: State) -> State:
        del state
        return {
            "artifact_ledger": fill_artifact_ledger(
                "intake",
                (_CASE,),
                (ArtifactRef(path="qa/cases/menus/case.yaml", digest="a" * 64),),
            )
        }

    def second(state: State) -> State:
        assert ledger_refs(state["artifact_ledger"], "intake.case")
        return {
            "artifact_ledger": {
                "intake.plan": [{"path": "qa/proposal.md", "digest": "c" * 64}],
            }
        }

    graph = StateGraph(State)
    graph.add_node("first", first)
    graph.add_node("second", second)
    graph.add_edge(START, "first")
    graph.add_edge("first", "second")
    graph.add_edge("second", END)
    result = graph.compile().invoke({})
    assert ledger_refs(result["artifact_ledger"], "intake.case")[0]["digest"] == "a" * 64
    assert result["artifact_ledger"]["intake.plan"][0]["path"] == "qa/proposal.md"


def test_accumulate_named_write_merges_by_path_before_the_reducer_replaces_the_key() -> None:
    write = NamedWrite(name="history", root="qa/history", many=True, accumulate=True)

    def publish(
        state: dict[str, object], output: object, receipt: object, *, committed: object = ()
    ) -> dict[str, object]:
        del state, output, receipt, committed
        return {}

    bound = bind_produced_artifacts(publish, namespace="intake", writes=(write,))
    first = bound(
        {},
        {},
        None,
        committed=(ArtifactRef(path="qa/history/a.md", digest="a" * 64),),
    )
    second = bound(
        {"artifact_ledger": first["artifact_ledger"]},
        {},
        None,
        committed=(
            ArtifactRef(path="qa/history/a.md", digest="b" * 64),
            ArtifactRef(path="qa/history/c.md", digest="c" * 64),
        ),
    )
    assert second["artifact_ledger"]["intake.history"] == [
        {"path": "qa/history/a.md", "digest": "b" * 64},
        {"path": "qa/history/c.md", "digest": "c" * 64},
    ]
    untouched = bound({"artifact_ledger": first["artifact_ledger"]}, {}, None, committed=())
    assert "artifact_ledger" not in untouched
    replaced = merge_artifact_ledger(
        second["artifact_ledger"],
        {"intake.history": [{"path": "qa/history/a.md", "digest": "d" * 64}]},
    )
    assert replaced["intake.history"] == [{"path": "qa/history/a.md", "digest": "d" * 64}]
