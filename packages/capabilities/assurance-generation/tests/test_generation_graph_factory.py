from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import fields
from pathlib import Path
from typing import Any

import pytest

from assurance_generation.contracts.attempts import AGENT_JOB_CONTRACTS, TASK_ATTEMPT_CONTRACTS
from assurance_generation.contracts.decisions import complete_generation
from assurance_generation.graphs.factory import (
    GenerationGraphs,
    build_generation_graphs as _build_generation_graphs,
)
from graph_engine.attempts.models.contracts import TaskAttemptContract
from graph_engine.attempts.models.resolutions import PermanentTaskFailure, ReceiptRef
from graph_engine.testing import GraphHarness, committed

from graph_engine.testing.feature_bundle import compile_bundle


def build_generation_graphs(*args, **kwargs):
    return compile_bundle(_build_generation_graphs(*args, **kwargs))


_SHA = "a" * 64
_RECEIPT_ID = "receipt-1"
_FAMILIES = ("api", "e2e", "fuzz", "performance")
_PHASE_NODES = frozenset(
    {
        "prepare",
        "execute",
        "finalize",
        "finalize-inputs",
        "repair-prepare",
        "repair-execute",
        "repair-finalize-inputs",
        "repair-finalize",
    }
)
_GRAPHS_ROOT = Path(__file__).resolve().parents[1] / "assurance_generation" / "graphs"


def _reviewed_case() -> dict[str, object]:
    plan_ref = {
        "path": f"qa/results/plan/{_SHA}/resolved-assurance-plan.json",
        "digest": _SHA,
    }
    return {
        "change_id": "CH-DEMO-001",
        "coverage_epoch": 0,
        "plan_digest": _SHA,
        "plan_ref": plan_ref,
        "preparation_refs": [
            {"path": "qa/requirement.md", "digest": _SHA},
            plan_ref,
        ],
        "case_refs": [{"path": "qa/cases/menus/case.yaml", "digest": _SHA}],
        "review_ref": {"path": "qa/results/review/case-review.json", "digest": _SHA},
        "selection_ref": {"path": "qa/results/cases/epochs/0/selection.json", "digest": _SHA},
    }


def generation_contracts() -> dict[str, TaskAttemptContract[Any, Any]]:
    contracts = {
        contract.contract_id: contract.to_task_contract() for contract in AGENT_JOB_CONTRACTS.values()
    }
    contracts.update({contract.contract_id: contract for contract in TASK_ATTEMPT_CONTRACTS.values()})
    return contracts


def generation_graph_input(
    *, selected: tuple[str, ...] = ("api", "e2e", "fuzz", "performance")
) -> dict[str, object]:
    return {
        "change_id": "CH-DEMO-001",
        "plan_digest": _SHA,
        "plan_ref": {
            "path": f"qa/results/plan/{_SHA}/resolved-assurance-plan.json",
            "digest": _SHA,
        },
        "selected_test_families": list(selected),
        "capability_leafs": ["entities.item.create"],
        "allowed_artifact_paths": [
            "qa/.qa.yaml",
            "qa/cases",
            "qa/fixtures",
            "qa/proposal.md",
            "qa/requirement.md",
            "qa/results",
            "qa/tests",
        ],
        "coverage_epoch": 0,
        "reviewed_case_ref": {
            "path": "qa/cases/reviewed-case.json",
            "digest": _SHA,
        },
    }


def _receipt() -> ReceiptRef:
    return ReceiptRef(receipt_id=_RECEIPT_ID, receipt_digest=_SHA)


def _review_output(route: str = "codegen") -> dict[str, object]:
    return {"route": route, "finding_ids": []}


def _published_cycle() -> dict[str, object]:
    return {"generation_result": {"change_id": "CH-DEMO-001", "coverage_epoch": 0}}


def _node_names(graph: object) -> set[str]:
    names: set[str] = set()
    nodes = getattr(graph, "nodes", {})
    if not isinstance(nodes, dict):
        return names
    for name, node in nodes.items():
        if name in {"__start__", "__end__"}:
            continue
        names.add(str(name))
        nested = getattr(node, "nodes", None)
        if nested is not None:
            names.update(_node_names(node))
        for attr in ("runnable", "bound"):
            child = getattr(node, attr, None)
            if child is not None and child is not graph:
                names.update(_node_names(child))
        subgraphs = getattr(node, "subgraphs", None)
        if isinstance(subgraphs, list):
            for subgraph in subgraphs:
                names.update(_node_names(subgraph))
    return names


def _walk_graph_python() -> Iterator[Path]:
    for path in sorted(_GRAPHS_ROOT.rglob("*.py")):
        if "__pycache__" not in path.parts:
            yield path


def _semantic(family: str, stage: str) -> str:
    return f"generation.{family}.{stage}"


def _script_selected(selected: tuple[str, ...], *, route: str = "codegen") -> dict[str, list[Any]]:
    receipt = _receipt()
    script: dict[str, list[Any]] = {
        "generation.resolve-inputs": [committed({"change_id": "CH-DEMO-001"}, receipt)],
        "generation.publish-cycle": [committed(_published_cycle(), receipt)],
    }
    for family in selected:
        script[_semantic(family, "codegen")] = [committed({"schema_version": "1"}, receipt)]
        script[_semantic(family, "codegen-review")] = [committed(_review_output(route), receipt)]
    return script


@pytest.fixture
def recording_context():
    return GraphHarness().recording_context(
        owner_id="assurance.generation",
        contracts=generation_contracts(),
    )


def test_generation_factory_exports_root_and_init(recording_context) -> None:
    bundle = build_generation_graphs(recording_context)
    assert tuple(item.name for item in fields(bundle)) == ("generation", "init_runtime")
    assert isinstance(bundle, GenerationGraphs)
    unique = tuple(dict.fromkeys(recording_context.bound_contract_ids))
    assert len(recording_context.bound_contract_ids) == 11
    assert recording_context.bound_contract_ids.count("assurance.generation.resolve-inputs") == 1
    assert set(unique) == {
        *(contract.contract_id for contract in AGENT_JOB_CONTRACTS.values()),
        *(contract.contract_id for contract in TASK_ATTEMPT_CONTRACTS.values()),
    }
    assert "assurance.generation.complete" not in unique
    assert "assurance.generation.review-round.advance" not in unique


def test_generation_factory_binds_family_semantic_ids(
    recording_context, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, str]] = []
    original = recording_context.attempt

    def record_attempt(
        contract_id: str,
        *,
        semantic_node_id: str,
        activation: object,
        select: object,
        publish: object,
    ) -> object:
        calls.append((contract_id, semantic_node_id))
        return original(
            contract_id,
            semantic_node_id=semantic_node_id,
            activation=activation,
            select=select,
            publish=publish,
        )

    monkeypatch.setattr(recording_context, "attempt", record_attempt)
    build_generation_graphs(recording_context)
    for family in _FAMILIES:
        assert (
            f"assurance.generation.agent.{family}.codegen.v1",
            f"generation.{family}.codegen",
        ) in calls
        assert (
            f"assurance.generation.agent.{family}.codegen-review.v1",
            f"generation.{family}.codegen-review",
        ) in calls


async def test_resolve_inputs_failure_stops_generation_before_fanout() -> None:
    harness = GraphHarness()
    bundle = build_generation_graphs(
        harness.recording_context(owner_id="assurance.generation", contracts=generation_contracts())
    )
    payload = generation_graph_input()
    payload["reviewed_case"] = None
    payload["source_artifacts"] = [{"path": "qa/cases/reviewed-case.json", "digest": _SHA}]
    result = await harness.run(
        bundle.generation,
        input=payload,
        script={
            "generation.resolve-inputs": [
                PermanentTaskFailure(kind="invalid_input", message="review digest changed")
            ]
        },
    )
    assert [call.semantic_node_id for call in result.semantic_calls] == ["generation.resolve-inputs"]
    assert isinstance(result.terminal, dict)
    assert result.terminal["status"] == "failed"
    assert result.terminal.get("reviewed_case") is None


def test_factory_binds_no_task_contract_for_pure_completion_or_round_advance(recording_context) -> None:
    build_generation_graphs(recording_context)
    assert "assurance.generation.complete" not in recording_context.bound_contract_ids
    assert "assurance.generation.review-round.advance" not in recording_context.bound_contract_ids
    assert AGENT_JOB_CONTRACTS.keys().isdisjoint({"complete", "review-round.advance"})


def test_root_factory_does_not_ainvoke_family_graphs(recording_context) -> None:
    source = (_GRAPHS_ROOT / "factory.py").read_text(encoding="utf-8")
    assert "ainvoke" not in source
    assert "compile_family_graph" not in source
    bundle = build_generation_graphs(recording_context)
    names = _node_names(bundle.generation)
    for family in _FAMILIES:
        assert f"__flow_branch__families__{family}" in names
    assert "codegen-human-review" not in names
    assert "human-review" in names or any("human-review" in name for name in names)


def test_complete_generation_still_requires_four_lane_tokens() -> None:
    complete_generation({"completed": [{"value": True}] * 4, "selected_families": ["api", "e2e"]})
    with pytest.raises((TypeError, ValueError)):
        complete_generation({"completed": [{"value": True}], "selected_families": ["api"]})


def test_target_graphs_contain_no_phase_nodes(recording_context) -> None:
    bundle = build_generation_graphs(recording_context)
    assert _node_names(bundle.generation).isdisjoint(_PHASE_NODES)
    for path in _walk_graph_python():
        source = path.read_text(encoding="utf-8")
        assert "capability_slot" not in source


async def test_selected_family_runs_codegen_and_review() -> None:
    harness = GraphHarness()
    context = harness.recording_context(owner_id="assurance.generation", contracts=generation_contracts())
    bundle = build_generation_graphs(context)
    result = await harness.run(
        bundle.generation,
        input=generation_graph_input(selected=("api",)),
        script=_script_selected(("api",)),
    )
    assert [call.semantic_node_id for call in result.semantic_calls] == [
        "generation.resolve-inputs",
        _semantic("api", "codegen"),
        _semantic("api", "codegen-review"),
        "generation.publish-cycle",
    ]
    assert isinstance(result.terminal, dict)
    assert result.terminal["status"] == "passed"
    assert "family_results" not in result.terminal


async def test_selected_family_codegen_failure_stops_before_review() -> None:
    harness = GraphHarness()
    context = harness.recording_context(owner_id="assurance.generation", contracts=generation_contracts())
    bundle = build_generation_graphs(context)
    script = _script_selected(("e2e",))
    script[_semantic("e2e", "codegen")] = [
        PermanentTaskFailure(kind="transient", message="provider TLS failed")
    ]
    result = await harness.run(
        bundle.generation,
        input=generation_graph_input(selected=("e2e",)),
        script=script,
    )
    assert [call.semantic_node_id for call in result.semantic_calls] == [
        "generation.resolve-inputs",
        _semantic("e2e", "codegen"),
    ]
    assert isinstance(result.terminal, Mapping)
    assert result.terminal["status"] == "failed"
    assert "family_results" not in result.terminal


async def test_unselected_families_are_not_invoked() -> None:
    harness = GraphHarness()
    context = harness.recording_context(owner_id="assurance.generation", contracts=generation_contracts())
    bundle = build_generation_graphs(context)
    result = await harness.run(
        bundle.generation,
        input=generation_graph_input(selected=("e2e",)),
        script=_script_selected(("e2e",)),
    )
    called = [call.semantic_node_id for call in result.semantic_calls]
    assert _semantic("e2e", "codegen") in called
    for family in ("api", "fuzz", "performance"):
        assert _semantic(family, "codegen") not in called
    assert isinstance(result.terminal, dict)
    assert result.terminal["status"] == "passed"


@pytest.mark.parametrize("family", _FAMILIES)
async def test_root_fails_closed_without_publishing_when_selected_family_attempt_fails(
    family: str,
) -> None:
    harness = GraphHarness()
    context = harness.recording_context(owner_id="assurance.generation", contracts=generation_contracts())
    bundle = build_generation_graphs(context)
    script = _script_selected((family,))
    script[_semantic(family, "codegen")] = [
        PermanentTaskFailure(kind="invalid_output", message="generated target is missing")
    ]
    result = await harness.run(
        bundle.generation,
        input=generation_graph_input(selected=(family,)),
        script=script,
    )
    assert isinstance(result.terminal, Mapping)
    assert result.terminal["status"] == "failed"
    assert "generation.publish-cycle" not in [call.semantic_node_id for call in result.semantic_calls]


def test_codegen_input_digest_changes_with_coverage_epoch() -> None:
    from assurance_generation.contracts.agent import CodegenInputV1
    from graph_engine.canonical import canonical_digest

    reviewed = _reviewed_case()
    base = {
        "change_id": "CH-DEMO-001",
        "plan_digest": _SHA,
        "plan_ref": reviewed["plan_ref"],
        "capability_leafs": ["entities.item.create"],
        "coverage_epoch": 0,
        "local_round": 0,
        "reviewed_case": reviewed,
    }
    first = CodegenInputV1.model_validate(base)
    second = CodegenInputV1.model_validate({**base, "coverage_epoch": 1})
    third = CodegenInputV1.model_validate({**base, "local_round": 1})
    digest_of = lambda model: canonical_digest(model.model_dump(mode="json"))  # noqa: E731
    assert digest_of(first) != digest_of(second)
    assert digest_of(first) != digest_of(third)
