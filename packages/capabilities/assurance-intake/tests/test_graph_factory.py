from __future__ import annotations

import ast
from collections.abc import Iterator
from dataclasses import fields
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from agent_runtime_contracts import AgentRuntimeCapabilities, CompositeAttemptExecutor, TypedPhaseBundle
from assurance_intake.contracts.agent import ArtifactListResultV1, CaseDesignInputV1
from assurance_intake.contracts.attempts import AGENT_JOB_CONTRACTS
from assurance_intake.graphs.factory import IntakeGraphs, build_intake_graphs
from graph_engine.attempts.context import AttemptExecutionContext
from graph_engine.attempts.contracts import TaskAttemptContract
from graph_engine.attempts.keys import AttemptKey
from graph_engine.attempts.resolutions import ReceiptRef
from graph_engine.plugin_api import ResourceClaims
from graph_engine.testing import GraphHarness, committed

_SHA = "a" * 64
_RECEIPT_ID = "receipt-1"
_INTAKE_ID = "assurance.intake.agent.intake.v1"
_EXPLORE_ID = "assurance.intake.agent.explore.v1"
_CASE_DESIGN_ID = "assurance.intake.agent.case-design.v1"
_CASE_REVIEW_ID = "assurance.intake.agent.case-review.v1"
_GRAPH_CONTRACT_IDS = (_INTAKE_ID, _EXPLORE_ID, _CASE_DESIGN_ID, _CASE_REVIEW_ID)
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
_GRAPHS_ROOT = Path(__file__).resolve().parents[1] / "assurance_intake" / "graphs"


def intake_contracts() -> dict[str, TaskAttemptContract[Any, Any]]:
    return {contract.contract_id: contract.to_task_contract() for contract in AGENT_JOB_CONTRACTS.values()}


def intake_graph_input() -> dict[str, object]:
    return {
        "change_id": "CH-DEMO-001",
        "requirement": "Cover department CRUD.",
        "selected_test_families": ["api"],
        "case_delta_paths": ["qa/changes/CH-DEMO-001/cases/menus/case.yaml"],
        "capability_leafs": ["entities.item.create"],
        "allowed_artifact_paths": ["qa/changes"],
        "rounds_used": 0,
        "rounds_budget": 2,
    }


def _receipt() -> ReceiptRef:
    return ReceiptRef(receipt_id=_RECEIPT_ID, receipt_digest=_SHA)


def _artifact_output() -> ArtifactListResultV1:
    return ArtifactListResultV1(output_files=("qa/changes/CH-DEMO-001/proposal.md",))


def _review_output(*, decision: str = "pass") -> dict[str, object]:
    return {
        "decision": decision,
        "auto_fix_allowed": False,
        "human_review_required": False,
        "artifacts": [{"path": "qa/changes", "digest": _SHA}],
        "rounds_used": 0,
        "rounds_budget": 2,
    }


def _design_output(*, validation_status: str = "pass") -> dict[str, object]:
    return {
        "output_files": ["qa/changes/CH-DEMO-001/proposal.md"],
        "validation_status": validation_status,
        "validation_attempt": 0 if validation_status == "pass" else 1,
        "validation_error": None if validation_status == "pass" else "authored cases failed validation",
        "artifacts": [{"path": "qa/changes/CH-DEMO-001/proposal.md", "digest": _SHA}],
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
        runnable = getattr(node, "runnable", None)
        if runnable is not None:
            names.update(_node_names(runnable))
    return names


def _walk_graph_python() -> Iterator[Path]:
    for path in sorted(_GRAPHS_ROOT.rglob("*.py")):
        if "__pycache__" not in path.parts:
            yield path


@pytest.fixture
def recording_context():
    return GraphHarness().recording_context(
        owner_id="assurance.intake",
        contracts=intake_contracts(),
    )


def test_intake_factory_exports_prepare_and_case(recording_context) -> None:
    bundle = build_intake_graphs(recording_context)
    assert tuple(item.name for item in fields(bundle)) == ("prepare", "case")
    assert isinstance(bundle, IntakeGraphs)
    assert recording_context.bound_contract_ids == (
        _INTAKE_ID,
        _EXPLORE_ID,
        _CASE_DESIGN_ID,
        _CASE_DESIGN_ID,
        _CASE_REVIEW_ID,
    )
    assert set(recording_context.bound_contract_ids) == set(_GRAPH_CONTRACT_IDS)
    assert recording_context.bound_contract_ids.count(_CASE_DESIGN_ID) == 2
    assert all(item is None for item in recording_context.compiled_subgraph_checkpointers)


def test_target_graphs_contain_no_phase_nodes_or_send(recording_context) -> None:
    bundle = build_intake_graphs(recording_context)
    names = _node_names(bundle.prepare) | _node_names(bundle.case)
    assert names.isdisjoint(_PHASE_NODES)
    send_hits: list[str] = []
    fanout_hits: list[str] = []
    for path in _walk_graph_python():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id == "Send":
                send_hits.append(f"{path}:{node.lineno}")
            if isinstance(node, ast.Name) and node.id in {"fanout", "min_matches"}:
                fanout_hits.append(f"{path}:{node.lineno}:{node.id}")
            if isinstance(node, ast.Attribute) and node.attr in {"Send", "fanout"}:
                send_hits.append(f"{path}:{node.lineno}:{node.attr}")
    assert send_hits == []
    assert fanout_hits == []


async def test_case_graph_runs_primary_and_repair_through_one_composite_attempt_each() -> None:
    harness = GraphHarness()
    context = harness.recording_context(owner_id="assurance.intake", contracts=intake_contracts())
    bundle = build_intake_graphs(context)
    receipt = _receipt()
    result = await harness.run(
        bundle.case,
        input=intake_graph_input(),
        script={
            "intake.case-design": [committed(_design_output(validation_status="needs_fix"), receipt)],
            "intake.case-design-repair": [committed(_design_output(validation_status="pass"), receipt)],
            "intake.case-review": [committed(_review_output(), receipt)],
        },
    )
    assert [call.semantic_node_id for call in result.semantic_calls] == [
        "intake.case-design",
        "intake.case-design-repair",
        "intake.case-review",
    ]
    assert [call.contract_id for call in result.semantic_calls] == [
        _CASE_DESIGN_ID,
        _CASE_DESIGN_ID,
        _CASE_REVIEW_ID,
    ]
    assert result.terminal is not None


class _RecordingPrepare:
    def __init__(self, prepared: dict[str, object]) -> None:
        self.prepared = prepared
        self.seen_input: CaseDesignInputV1 | None = None

    async def execute(self, validated_input: CaseDesignInputV1, context: object) -> dict[str, object]:
        del context
        self.seen_input = validated_input
        return self.prepared


class _RecordingRuntime:
    def __init__(self, result: ArtifactListResultV1) -> None:
        self.result = result
        self.seen_prepared: dict[str, object] | None = None

    async def execute(self, prepared: dict[str, object], context: object, *, schema: object) -> object:
        del context, schema
        self.seen_prepared = prepared
        return self.result


class _RecordingFinalize:
    def __init__(self, output: ArtifactListResultV1) -> None:
        self.output = output
        self.seen: TypedPhaseBundle[dict[str, object], ArtifactListResultV1] | None = None

    async def execute(
        self,
        bundle: TypedPhaseBundle[dict[str, object], ArtifactListResultV1],
        context: object,
    ) -> ArtifactListResultV1:
        del context
        self.seen = bundle
        return self.output


def _attempt_context(semantic_node_id: str) -> AttemptExecutionContext:
    return AttemptExecutionContext(
        invocation_id="inv-1",
        public_entrypoint="case",
        semantic_node_id=semantic_node_id,
        attempt_key=AttemptKey(digest=_SHA),
        fencing_token=1,
    )


def _case_design_input(*, validation_attempt: int = 0) -> CaseDesignInputV1:
    payload: dict[str, object] = {
        "change_id": "CH-DEMO-001",
        "capability_leafs": ["entities.item.create"],
        "artifact_paths": ["qa/changes"],
        "selected_test_families": ["api"],
        "case_delta_paths": ["qa/changes/CH-DEMO-001/cases/menus/case.yaml"],
        "validation_attempt": validation_attempt,
    }
    if validation_attempt == 1:
        payload["validation_error"] = "authored cases failed validation"
    return CaseDesignInputV1.model_validate(payload)


@pytest.mark.parametrize(
    ("path", "semantic_node_id", "validation_attempt"),
    [
        ("primary", "intake.case-design", 0),
        ("repair", "intake.case-design-repair", 1),
    ],
)
async def test_prepared_value_and_agent_result_reach_finalize_through_one_composite_attempt(
    path: str, semantic_node_id: str, validation_attempt: int
) -> None:
    del path
    prepared: dict[str, object] = {"prompt": "design cases", "path": semantic_node_id}
    agent_result = ArtifactListResultV1(output_files=("qa/changes/CH-DEMO-001/proposal.md",))
    prepare = _RecordingPrepare(prepared)
    runtime = _RecordingRuntime(agent_result)
    finalize = _RecordingFinalize(agent_result)
    contract = AGENT_JOB_CONTRACTS["case-design"]
    executor = CompositeAttemptExecutor(
        contract,
        prepare=prepare,
        runtime=runtime,
        finalize=finalize,
        capabilities=AgentRuntimeCapabilities(provider_schema=True),
    )
    writable = ResourceClaims()
    del writable
    output = await executor.execute(
        _case_design_input(validation_attempt=validation_attempt),
        _attempt_context(semantic_node_id),
    )
    assert prepare.seen_input is not None
    assert prepare.seen_input.validation_attempt == validation_attempt
    assert runtime.seen_prepared == prepared
    assert finalize.seen is not None
    assert finalize.seen.prepared == prepared
    assert finalize.seen.agent_result == agent_result
    assert output == agent_result
    assert isinstance(output, BaseModel)


async def test_prepare_graph_binds_four_agent_ids_across_five_occurrences() -> None:
    harness = GraphHarness()
    context = harness.recording_context(owner_id="assurance.intake", contracts=intake_contracts())
    bundle = build_intake_graphs(context)
    receipt = _receipt()
    result = await harness.run(
        bundle.prepare,
        input=intake_graph_input(),
        script={
            "intake.intake": [committed(_artifact_output(), receipt)],
            "intake.explore": [committed(_artifact_output(), receipt)],
            "intake.case-design": [committed(_design_output(), receipt)],
            "intake.case-review": [committed(_review_output(), receipt)],
        },
    )
    assert [call.semantic_node_id for call in result.semantic_calls] == [
        "intake.intake",
        "intake.explore",
        "intake.case-design",
        "intake.case-review",
    ]
    assert [call.contract_id for call in result.semantic_calls] == [
        _INTAKE_ID,
        _EXPLORE_ID,
        _CASE_DESIGN_ID,
        _CASE_REVIEW_ID,
    ]
    assert result.promotion_decision == "committed"
    terminal = result.terminal
    assert isinstance(terminal, dict)
    assert terminal.get("decision") in {"pass", "approved"}
