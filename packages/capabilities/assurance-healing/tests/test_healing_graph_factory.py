from __future__ import annotations

import ast
from collections.abc import Iterator
from dataclasses import fields
from pathlib import Path
from typing import Any

import pytest

from assurance_healing.contracts.attempts import AGENT_JOB_CONTRACTS
from assurance_healing.graphs.factory import HealingGraphs, build_healing_graphs as _build_healing_graphs
from graph_engine.attempts.contracts import TaskAttemptContract
from graph_engine.attempts.resolutions import ReceiptRef
from graph_engine.testing import GraphHarness, committed

from graph_engine.testing.feature_bundle import compile_bundle


def build_healing_graphs(*args, **kwargs):
    return compile_bundle(_build_healing_graphs(*args, **kwargs))


_SHA = "a" * 64
_RECEIPT_ID = "receipt-1"
_FIX_PROPOSAL_ID = "assurance.healing.agent.fix-proposal.v1"
_APPLICATION_ID = "assurance.healing.agent.apply-test-repair.v1"
_GRAPH_CONTRACT_IDS = (_APPLICATION_ID, _FIX_PROPOSAL_ID)
_ADVANCE_ID = "assurance.healing.repair-round.advance"
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


def failure_graph_input(**overrides: object) -> dict[str, object]:
    plan_ref = {
        "path": f"qa/results/plan/{_SHA}/resolved-assurance-plan.json",
        "digest": _SHA,
    }
    payload: dict[str, object] = {
        "change_id": "CH-FIX-001",
        "plan_digest": _SHA,
        "plan_ref": plan_ref,
        "capability_leafs": ["entities.item.create"],
        "coverage_epoch": 0,
        "repair_round": 1,
        "product_policy": {
            "resource_id": "assurance.product.configuration.product-policy",
            "sha256": "d" * 64,
        },
        "generation_ref": {
            "path": "qa/results/codegen/generation-cycle.json",
            "digest": _SHA,
        },
        "execution_ref": {
            "path": "qa/results/execution/execution-cycle.json",
            "digest": _SHA,
        },
        "execution_receipt": {"receipt_id": "execute", "receipt_digest": "c" * 64},
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


def application_output() -> dict[str, object]:
    return {
        "change_id": "CH-FIX-001",
        "plan_digest": _SHA,
        "plan_ref": {
            "path": f"qa/results/plan/{_SHA}/resolved-assurance-plan.json",
            "digest": _SHA,
        },
        "coverage_epoch": 0,
        "repair_round": 1,
        "changed_test_refs": [
            {
                "path": "qa/tests/api/test_items.py",
                "digest": _SHA,
            }
        ],
        "mapping_ref": {
            "path": "qa/results/generated/mapping.json",
            "digest": _SHA,
        },
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


def test_healing_factory_exports_the_failure_graph(
    recording_context, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, str, object, object, object]] = []
    original = recording_context.attempt

    def record_attempt(
        contract_id: str,
        *,
        semantic_node_id: str,
        activation: object,
        select: object,
        publish: object,
    ) -> object:
        calls.append((contract_id, semantic_node_id, activation, select, publish))
        return original(
            contract_id,
            semantic_node_id=semantic_node_id,
            activation=activation,
            select=select,
            publish=publish,
        )

    monkeypatch.setattr(recording_context, "attempt", record_attempt)
    bundle = build_healing_graphs(recording_context)
    assert tuple(item.name for item in fields(bundle)) == ("repair_failure",)
    assert isinstance(bundle, HealingGraphs)
    assert not hasattr(bundle, "nodes")
    assert set(recording_context.bound_contract_ids) == set(_GRAPH_CONTRACT_IDS)
    assert recording_context.bound_contract_ids.count(_FIX_PROPOSAL_ID) == 1
    assert recording_context.bound_contract_ids.count(_APPLICATION_ID) == 1
    assert _ADVANCE_ID not in recording_context.bound_contract_ids
    assert all(item is None for item in recording_context.compiled_subgraph_checkpointers)
    assert len(calls) == 2
    assert {(row[0], row[1]) for row in calls} == {
        (_FIX_PROPOSAL_ID, "healing.fix-proposal"),
        (_APPLICATION_ID, "healing.apply-test-repair"),
    }


def test_target_graphs_contain_no_phase_nodes_or_send(recording_context) -> None:
    bundle = build_healing_graphs(recording_context)
    names = _node_names(bundle.repair_failure)
    assert names.isdisjoint(_PHASE_NODES)
    for path in _walk_graph_python():
        source = path.read_text(encoding="utf-8")
        assert "capability_slot" not in source
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                assert node.id != "Send"


async def test_repair_exports_run_independently_and_publish_typed_output() -> None:
    harness = GraphHarness()
    context = harness.recording_context(owner_id="assurance.healing", contracts=healing_contracts())
    bundle = build_healing_graphs(context)
    receipt = _receipt()
    failure = await harness.run(
        bundle.repair_failure,
        input=failure_graph_input(),
        script={
            "healing.fix-proposal": [
                committed(
                    failure_agent_output(),
                    receipt,
                    artifacts=[{"path": "qa/results/healing/fix-proposal.json", "digest": _SHA}],
                )
            ],
            "healing.apply-test-repair": [committed(application_output(), receipt)],
        },
    )
    assert failure.interrupt_envelope is None
    assert [call.semantic_node_id for call in failure.semantic_calls] == [
        "healing.fix-proposal",
        "healing.apply-test-repair",
    ]
    assert [call.contract_id for call in failure.semantic_calls] == [_FIX_PROPOSAL_ID, _APPLICATION_ID]
    assert isinstance(failure.terminal, dict)
    assert failure.terminal["status"] == "applied"


def test_production_agent_validators_stay_empty() -> None:
    assert all(contract.validators == () for contract in AGENT_JOB_CONTRACTS.values())
