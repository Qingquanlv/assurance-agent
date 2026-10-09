from __future__ import annotations

import ast
from collections.abc import Iterator
from dataclasses import fields
from pathlib import Path
from typing import Any, cast

import pytest

from assurance_improvement.contracts.attempts import AGENT_JOB_CONTRACTS, TASK_ATTEMPT_CONTRACTS
from assurance_improvement.graphs.factory import (
    ImprovementGraphs,
    build_improvement_graphs as _build_improvement_graphs,
)
from graph_engine.attempts.models.contracts import TaskAttemptContract
from graph_engine.attempts.models.resolutions import ReceiptRef
from graph_engine.testing import GraphHarness, committed

from graph_engine.testing.feature_bundle import compile_bundle


def build_improvement_graphs(*args, **kwargs):
    return compile_bundle(_build_improvement_graphs(*args, **kwargs))


_SHA = "a" * 64
_RECEIPT_ID = "receipt-1"
_ARCHIVE_ID = "assurance.improvement.agent.archive.v1"
_REVIEW_ID = "assurance.improvement.agent.improvement-review.v1"
_RETRO_EVAL_ID = "assurance.improvement.agent.retro-eval-analysis.v1"
_RETRO_ISSUE_ID = "assurance.improvement.agent.retro-issue-analysis.v1"
_RETRO_WORKFLOW_ID = "assurance.improvement.agent.retro-workflow-analysis.v1"
_RETRO_ID = "assurance.improvement.agent.retro.v1"
AGENT_IDS = (
    _ARCHIVE_ID,
    _REVIEW_ID,
    _RETRO_EVAL_ID,
    _RETRO_ISSUE_ID,
    _RETRO_WORKFLOW_ID,
    _RETRO_ID,
)
TASK_COLLECT_ID = "assurance.improvement.task.retro-collect-v3"
TASK_BUILD_SLICES_ID = "assurance.improvement.retro-build-slices"
TASK_RECONCILE_ID = "assurance.improvement.task.reconcile-improvements"
TASK_AUTO_REVIEW_ID = "assurance.improvement.task.apply-improvement-auto-review"
TASK_HUMAN_REVIEW_ID = "assurance.improvement.task.apply-improvement-review"
TASK_EVALUATE_ID = "assurance.improvement.task.evaluate-memory-improvement"
TASK_EXPORT_ID = "assurance.improvement.task.export-change-improvement"
TASK_APPLY_ID = "assurance.improvement.task.apply-memory-improvement"
TASK_ROLLBACK_ID = "assurance.improvement.task.rollback-memory-improvement"
TASK_SNAPSHOT_ID = "assurance.improvement.task.retro-runtime-snapshot"
TASK_IDS = (
    TASK_BUILD_SLICES_ID,
    TASK_COLLECT_ID,
    TASK_RECONCILE_ID,
    TASK_AUTO_REVIEW_ID,
    TASK_HUMAN_REVIEW_ID,
    TASK_EVALUATE_ID,
    TASK_EXPORT_ID,
    TASK_APPLY_ID,
    TASK_ROLLBACK_ID,
    TASK_SNAPSHOT_ID,
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
_GRAPHS_ROOT = Path(__file__).resolve().parents[1] / "assurance_improvement" / "graphs"
_FORBIDDEN_GRAPHS = ("benchmark_eval", "nightly", "benchmark-eval", "eval")


def improvement_contracts() -> dict[str, TaskAttemptContract[Any, Any]]:
    contracts: dict[str, TaskAttemptContract[Any, Any]] = {}
    for job in AGENT_JOB_CONTRACTS.values():
        contracts[job.contract_id] = job.to_task_contract()
    for task in TASK_ATTEMPT_CONTRACTS.values():
        contracts[task.contract_id] = task
    return contracts


def _receipt() -> ReceiptRef:
    return ReceiptRef(receipt_id=_RECEIPT_ID, receipt_digest=_SHA)


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


def _bundle_graphs(bundle: ImprovementGraphs) -> tuple[object, ...]:
    return (
        bundle.archive,
        bundle.retro,
        bundle.review,
        bundle.evaluate,
        bundle.export,
        bundle.apply,
        bundle.rollback,
    )


@pytest.fixture
def recording_context():
    return GraphHarness().recording_context(
        owner_id="assurance.improvement",
        contracts=improvement_contracts(),
    )


def test_improvement_factory_exports_seven_public_graphs(recording_context) -> None:
    bundle = build_improvement_graphs(recording_context)
    assert tuple(item.name for item in fields(bundle)) == (
        "archive",
        "retro",
        "review",
        "evaluate",
        "export",
        "apply",
        "rollback",
        "runtime_snapshot",
    )
    assert tuple(ImprovementGraphs.__dataclass_fields__) == (
        "archive",
        "retro",
        "review",
        "evaluate",
        "export",
        "apply",
        "rollback",
        "runtime_snapshot",
    )
    assert isinstance(bundle, ImprovementGraphs)
    assert not hasattr(bundle, "nodes")
    for name in _FORBIDDEN_GRAPHS:
        assert not hasattr(bundle, name)
    bound = recording_context.bound_contract_ids
    assert len(bound) == 18
    assert bound.count("assurance.improvement.retro-build-slices") == 1
    assert set(bound) >= set(AGENT_IDS)
    assert set(bound) >= set(TASK_IDS)
    assert len({item for item in bound if item in AGENT_IDS}) == 6
    assert len({item for item in bound if item in TASK_IDS}) == 10
    assert bound.count(TASK_EVALUATE_ID) == 2
    assert sum(1 for item in bound if item in TASK_IDS) == 11
    for agent_id in AGENT_IDS:
        assert bound.count(agent_id) == 1
    assert "assurance.improvement.graph.benchmark-eval" not in bound
    assert "assurance.improvement.evaluate-benchmark" not in bound
    assert all(item is None for item in recording_context.compiled_subgraph_checkpointers)


def test_target_graphs_contain_no_phase_nodes_or_send(recording_context) -> None:
    bundle = build_improvement_graphs(recording_context)
    names: set[str] = set()
    for graph in _bundle_graphs(bundle):
        names.update(_node_names(graph))
    assert names.isdisjoint(_PHASE_NODES)
    for path in _walk_graph_python():
        source = path.read_text(encoding="utf-8")
        assert "capability_slot" not in source
        assert "benchmark" not in source.lower() or "benchmark" not in path.name
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                assert node.id != "Send"


def test_production_agent_and_task_validators_stay_empty() -> None:
    assert all(contract.validators == () for contract in AGENT_JOB_CONTRACTS.values())
    assert all(contract.validators == () for contract in TASK_ATTEMPT_CONTRACTS.values())


def skill_graph_fields() -> dict[str, object]:
    return {
        "change_id": "CH-DEMO-001",
        "retro_id": "RET-1",
        "owned_evidence_ids": ["PROB-1", "OCC-1"],
        "artifact_paths": ["retro/context.json"],
        "allowed_artifact_paths": [
            "qa/.qa.yaml",
            "qa/cases",
            "qa/fixtures",
            "qa/proposal.md",
            "qa/requirement.md",
            "qa/results",
            "qa/tests",
        ],
        "capability_leafs": ["auth.session.create"],
        "source_manifest": {
            "issue_slice_sha256": "a",
            "workflow_slice_sha256": "b",
            "eval_slice_sha256": "c",
            "issue_sources": [
                {
                    "kind": "project_problem_ledger",
                    "change_id": None,
                    "head_event_id": "evt-1",
                    "sha256": "abc",
                    "evidence_ids": ["PROB-1", "OCC-1"],
                }
            ],
            "workflow_sources": [],
            "eval_sources": [],
        },
        "context_digest": _SHA,
        "quality_report_digest": "b" * 64,
        "metrics_digest": _SHA,
        "issue_digest": "b" * 64,
        "subject_digest": _SHA,
        "expected_improvement_version": 1,
        "improvement_id": "IMP-1",
        "invocation_id": "inv-archive-1",
        "archive_digest": _SHA,
        "locked_signal_ids": ["issue-pattern:PROB-1"],
        "evidence_refs": [{"path": "qa/results/inspect/inspection.json", "digest": _SHA}],
        "lifecycle_state": "exported",
    }


def archive_graph_input(**overrides: object) -> dict[str, object]:
    payload = skill_graph_fields()
    payload.update(overrides)
    return payload


def archive_agent_output() -> dict[str, object]:
    return {
        "schema_version": "1",
        "change_id": "CH-DEMO-001",
        "archive_status": "archived",
        "issue_risk": "clear",
        "issue_risk_rationale": "no active issues",
        "summary": "# Archive CH-DEMO-001\n",
        "artifact_paths": ["qa/results/archive-summary.md"],
        "invocation_id": "inv-archive-1",
        "archive_digest": _SHA,
        "lifecycle_state": None,
        "evidence_refs": [],
    }


async def test_archive_export_is_independent_of_retro() -> None:
    harness = GraphHarness()
    context = harness.recording_context(
        owner_id="assurance.improvement",
        contracts=improvement_contracts(),
    )
    bundle = build_improvement_graphs(context)
    output = archive_agent_output()
    result = await harness.run(
        bundle.archive,
        input=archive_graph_input(),
        script={"improvement.archive": [committed(output, _receipt())]},
    )
    assert [call.semantic_node_id for call in result.semantic_calls] == ["improvement.archive"]
    assert [call.contract_id for call in result.semantic_calls] == [_ARCHIVE_ID]
    assert result.terminal is not None
    assert all("retro" not in call.semantic_node_id for call in result.semantic_calls)
    assert output["archive_status"] == "archived"
    assert cast(dict[str, object], result.terminal)["status"] == "done"
