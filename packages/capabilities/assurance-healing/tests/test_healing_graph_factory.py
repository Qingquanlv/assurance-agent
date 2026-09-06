from __future__ import annotations

import ast
import hashlib
from collections.abc import Iterator
from dataclasses import fields
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError

from assurance_healing.contracts.attempts import AGENT_JOB_CONTRACTS
from assurance_healing.contracts.decisions import HealingRepairRoundAdvanceOutput, advance_repair_round
from assurance_healing.graphs.factory import HealingGraphs, build_healing_graphs
from assurance_healing.graphs.nodes import activation_repair, advance_repair_round_node
from graph_engine.attempts.contracts import TaskAttemptContract
from graph_engine.attempts.keys import BusinessActivation
from graph_engine.attempts.resolutions import ReceiptRef
from graph_engine.canonical import JSONValue, canonical_json_bytes
from graph_engine.testing import GraphHarness, committed

_SHA = "a" * 64
_RECEIPT_ID = "receipt-1"
_COVERAGE_ID = "assurance.healing.agent.coverage-repair.v1"
_FIX_PROPOSAL_ID = "assurance.healing.agent.fix-proposal.v1"
_APPLICATION_ID = "assurance.healing.agent.apply-test-repair.v1"
_GRAPH_CONTRACT_IDS = (_APPLICATION_ID, _COVERAGE_ID, _FIX_PROPOSAL_ID)
_ADVANCE_ID = "assurance.healing.repair-round.advance"
EFFECT_IDS = (
    "assurance.healing.effect.allocation.v2",
    "assurance.healing.effect.heal-apply.v2",
    "assurance.healing.effect.proposal-approved.v1",
)
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
_GRAPHS_ROOT = Path(__file__).resolve().parents[1] / "assurance_healing" / "graphs"


def healing_contracts() -> dict[str, TaskAttemptContract[Any, Any]]:
    return {contract.contract_id: contract.to_task_contract() for contract in AGENT_JOB_CONTRACTS.values()}


def _effect_refs() -> list[dict[str, str]]:
    return [{"kind": kind, "digest": _SHA} for kind in EFFECT_IDS]


def _coverage_brief(*, change_id: str = "CH-COV-002") -> dict[str, object]:
    return {
        "schema_version": "1",
        "change_id": change_id,
        "batch_id": "20260822T000000Z",
        "probe_verdict": "needs_human",
        "eligible": True,
        "allowed_test_files": ["tests/api/test_users.py"],
        "repair_items": [
            {
                "kind": "uncovered_required_case",
                "locator": {"case_id": "C1"},
                "metric": "required_case_coverage",
            }
        ],
    }


def failure_graph_input(**overrides: object) -> dict[str, object]:
    source = "qa/changes/CH-FIX-001/generated/api/files/tests/api/test_items.py"
    plan_ref = {
        "path": f"qa/changes/CH-FIX-001/plan/{_SHA}/resolved-assurance-plan.json",
        "digest": _SHA,
    }
    payload: dict[str, object] = {
        "change_id": "CH-FIX-001",
        "plan_digest": _SHA,
        "plan_ref": plan_ref,
        "capability_leafs": ["entities.item.create"],
        "allowed_artifact_paths": ["qa/changes"],
        "classification": "test",
        "fix_eligible": True,
        "kind": "failure",
        "rounds_budget": 2,
        "rounds_used": 0,
        "budgets": {"coverage_rounds": 2, "failure_rounds": 1},
        "activation": {"kind": "round", "value": "1"},
        "owner_id": "assurance.healing",
        "allowed_paths": ["qa/changes"],
        "allowed_roots": ["qa"],
        "baseline_digest": "b" * 64,
        "candidate_digest": "c" * 64,
        "policy_digest": "d" * 64,
        "mapping_paths": ["qa/changes/CH-FIX-001/plans/api-codegen-mapping.json"],
        "execution_evidence_digest": "e" * 64,
        "coverage_epoch": 0,
        "reviewed_case": {
            "change_id": "CH-FIX-001",
            "coverage_epoch": 0,
            "plan_digest": _SHA,
            "plan_ref": plan_ref,
            "preparation_refs": [
                {"path": "qa/changes/CH-FIX-001/intake/prepare.json", "digest": _SHA},
                plan_ref,
            ],
            "case_refs": [{"path": "qa/changes/CH-FIX-001/cases/api/case.yaml", "digest": _SHA}],
            "review_ref": {
                "path": "qa/changes/CH-FIX-001/review/case-review.json",
                "digest": _SHA,
            },
        },
        "proposal_ref": {
            "path": "qa/changes/CH-FIX-001/healing/fix-proposal.json",
            "digest": _SHA,
        },
        "approval_ref": {
            "path": "qa/changes/CH-FIX-001/healing/approval.json",
            "digest": _SHA,
        },
        "execution_ref": {
            "path": "qa/changes/CH-FIX-001/execution/execute-result.json",
            "digest": _SHA,
        },
        "mapping_ref": {
            "path": "qa/changes/CH-FIX-001/generated/mapping.json",
            "digest": _SHA,
        },
        "source_refs": [{"path": source, "digest": _SHA}],
        "allowed_test_paths": [source],
    }
    payload.update(overrides)
    return payload


def coverage_graph_input(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "change_id": "CH-COV-002",
        "capability_leafs": ["auth.session.create"],
        "allowed_artifact_paths": ["qa/archive"],
        "classification": "repair_required",
        "fix_eligible": True,
        "kind": "coverage",
        "rounds_budget": 4,
        "rounds_used": 1,
        "budgets": {"coverage_rounds": 9, "failure_rounds": 4},
        "activation": {"kind": "round", "value": "2"},
        "baseline_digest": "b" * 64,
        "allowed_roots": ["qa"],
        "brief": _coverage_brief(),
    }
    payload.update(overrides)
    return payload


def _receipt() -> ReceiptRef:
    return ReceiptRef(receipt_id=_RECEIPT_ID, receipt_digest=_SHA)


def failure_agent_output() -> dict[str, object]:
    return {
        "schema_version": "1",
        "change_id": "CH-FIX-001",
        "summary": {"eligible_count": 1},
        "proposals": [],
    }


def test_proposal_publisher_exposes_the_committed_proposal_reference() -> None:
    from assurance_healing.graphs.nodes import publish_proposal

    output = failure_agent_output()
    published = publish_proposal(failure_graph_input(), output, _receipt())
    assert published["proposal_ref"] == {
        "path": "qa/changes/CH-FIX-001/healing/fix-proposal.json",
        "digest": hashlib.sha256(canonical_json_bytes(cast(JSONValue, output)) + b"\n").hexdigest(),
    }


def application_output() -> dict[str, object]:
    return {
        "change_id": "CH-FIX-001",
        "plan_digest": _SHA,
        "plan_ref": {
            "path": f"qa/changes/CH-FIX-001/plan/{_SHA}/resolved-assurance-plan.json",
            "digest": _SHA,
        },
        "coverage_epoch": 0,
        "repair_round": 1,
        "changed_test_refs": [
            {
                "path": "qa/changes/CH-FIX-001/generated/api/files/tests/api/test_items.py",
                "digest": _SHA,
            }
        ],
        "mapping_ref": {
            "path": "qa/changes/CH-FIX-001/generated/mapping.json",
            "digest": _SHA,
        },
    }


def coverage_agent_output(*, status: str = "repaired") -> dict[str, object]:
    return {
        "schema_version": "1",
        "change_id": "CH-COV-002",
        "status": status,
        "attempts_used": 1,
        "effect_refs": [
            {"kind": "assurance.healing.effect.allocation.v2", "digest": _SHA},
            {"kind": "assurance.healing.effect.heal-apply.v2", "digest": _SHA},
        ],
    }


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


@pytest.fixture
def recording_context():
    return GraphHarness().recording_context(
        owner_id="assurance.healing",
        contracts=healing_contracts(),
    )


def test_healing_factory_exports_two_independent_graphs(recording_context) -> None:
    bundle = build_healing_graphs(recording_context)
    assert tuple(item.name for item in fields(bundle)) == ("repair_failure", "repair_coverage")
    assert isinstance(bundle, HealingGraphs)
    assert not hasattr(bundle, "nodes")
    assert set(recording_context.bound_contract_ids) == set(_GRAPH_CONTRACT_IDS)
    assert recording_context.bound_contract_ids.count(_FIX_PROPOSAL_ID) == 1
    assert recording_context.bound_contract_ids.count(_APPLICATION_ID) == 1
    assert recording_context.bound_contract_ids.count(_COVERAGE_ID) == 1
    assert _ADVANCE_ID not in recording_context.bound_contract_ids
    assert all(item is None for item in recording_context.compiled_subgraph_checkpointers)


def test_target_graphs_contain_no_phase_nodes_or_send(recording_context) -> None:
    bundle = build_healing_graphs(recording_context)
    names = _node_names(bundle.repair_failure) | _node_names(bundle.repair_coverage)
    assert names.isdisjoint(_PHASE_NODES)
    for path in _walk_graph_python():
        source = path.read_text(encoding="utf-8")
        assert "capability_slot" not in source
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                assert node.id != "Send"


def test_failure_and_coverage_inputs_are_independent() -> None:
    from assurance_healing.graphs.nodes import select_coverage, select_failure

    failure = failure_graph_input()
    coverage = coverage_graph_input()
    selected_failure = select_failure(failure)
    selected_coverage = select_coverage(coverage)
    assert selected_failure.change_id == "CH-FIX-001"
    assert selected_coverage.change_id == "CH-COV-002"
    assert selected_failure.change_id != selected_coverage.change_id
    with pytest.raises((ValidationError, ValueError, TypeError, KeyError)):
        select_failure(coverage)
    with pytest.raises((ValidationError, ValueError, TypeError, KeyError)):
        select_coverage(failure)


def test_effectful_activation_uses_kind_and_parent_round_or_trigger() -> None:
    failure = activation_repair({"kind": "failure", "activation": {"kind": "round", "value": "1"}})
    coverage = activation_repair({"kind": "coverage", "activation": {"kind": "round", "value": "2"}})
    trigger = activation_repair({"kind": "coverage", "current_trigger": {"arrival_id": "cov.arr.1"}})
    assert failure == BusinessActivation.for_round(1)
    assert coverage == BusinessActivation.for_round(2)
    assert trigger == BusinessActivation.for_trigger("cov.arr.1")
    assert failure != BusinessActivation.one_shot()
    assert coverage != BusinessActivation.one_shot()
    assert trigger != BusinessActivation.one_shot()
    with pytest.raises((ValidationError, ValueError, TypeError, KeyError)):
        activation_repair({"activation": {"kind": "round", "value": "1"}})
    with pytest.raises((ValidationError, ValueError, TypeError, KeyError)):
        activation_repair({"kind": "failure"})
    with pytest.raises((ValidationError, ValueError, TypeError, KeyError)):
        activation_repair({"kind": "failure", "activation": {"kind": "root", "value": "1"}})


def test_advance_repair_round_node_is_the_moved_pure_function() -> None:
    payload = {"kind": "failure", "rounds_used": 0, "rounds_budget": 2}
    output = advance_repair_round_node(payload)
    expected = advance_repair_round(payload)
    assert isinstance(expected, HealingRepairRoundAdvanceOutput)
    assert output == expected.model_dump(mode="json")
    assert output["rounds_used"] == 1
    assert output["rounds_budget"] == 2
    assert output["kind"] == "failure"
    with pytest.raises((ValidationError, ValueError)):
        advance_repair_round_node({"kind": "failure", "rounds_used": 2, "rounds_budget": 2})
    with pytest.raises((ValidationError, ValueError)):
        advance_repair_round_node({"kind": "unknown", "rounds_used": 0, "rounds_budget": 2})


async def test_repair_exports_run_independently_and_publish_typed_output() -> None:
    harness = GraphHarness()
    context = harness.recording_context(owner_id="assurance.healing", contracts=healing_contracts())
    bundle = build_healing_graphs(context)
    receipt = _receipt()
    failure = await harness.run(
        bundle.repair_failure,
        input=failure_graph_input(),
        script={
            "healing.fix-proposal": [committed(failure_agent_output(), receipt)],
            "healing.apply-test-repair": [committed(application_output(), receipt)],
        },
    )
    assert [call.semantic_node_id for call in failure.semantic_calls] == [
        "healing.fix-proposal",
        "healing.apply-test-repair",
    ]
    assert [call.contract_id for call in failure.semantic_calls] == [
        _FIX_PROPOSAL_ID,
        _APPLICATION_ID,
    ]
    published = failure.published_update
    assert published is not None
    assert published["change_id"] == "CH-FIX-001"
    assert published["kind"] == "failure"
    assert published["status"] == "applied"
    assert published["rounds_used"] == 1
    assert published["rounds_budget"] == 2
    refs = published["effect_refs"]
    assert isinstance(refs, list)
    assert refs == []
    repair_result = cast(dict[str, object], published["repair_result"])
    assert repair_result["receipt"] == receipt.model_dump(mode="json")
    assert failure.terminal is not None

    coverage = await harness.run(
        bundle.repair_coverage,
        input=coverage_graph_input(),
        script={"healing.coverage-repair": [committed(coverage_agent_output(), receipt)]},
    )
    assert [call.semantic_node_id for call in coverage.semantic_calls] == ["healing.coverage-repair"]
    assert [call.contract_id for call in coverage.semantic_calls] == [_COVERAGE_ID]
    published_coverage = coverage.published_update
    assert published_coverage is not None
    assert published_coverage["change_id"] == "CH-COV-002"
    assert published_coverage["kind"] == "coverage"
    assert published_coverage["status"] == "repaired"
    assert published_coverage["rounds_used"] == 2
    assert published_coverage["rounds_budget"] == 4
    assert coverage.terminal is not None


def test_production_agent_validators_stay_empty() -> None:
    assert all(contract.validators == () for contract in AGENT_JOB_CONTRACTS.values())
