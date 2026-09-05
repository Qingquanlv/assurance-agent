from __future__ import annotations

import ast
from collections.abc import Iterator
from dataclasses import fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError

from assurance_quality.contracts.attempts import AGENT_JOB_CONTRACTS, TASK_ATTEMPT_CONTRACTS
from assurance_quality.contracts.metrics import (
    METRIC_KEYS,
    MetricEntry,
    MetricKey,
    MetricScope,
    MetricsDocument,
)
from assurance_quality.contracts.sufficiency import TraceSufficiencyFacts
from assurance_quality.graphs.factory import QualityGraphs, build_quality_graphs
from assurance_quality.operations.metrics import BoundRisk
from graph_engine.attempts.contracts import TaskAttemptContract
from graph_engine.attempts.keys import BusinessActivation
from graph_engine.attempts.resolutions import PermanentTaskFailure, ReceiptRef
from graph_engine.testing import GraphHarness, committed

_SHA = "a" * 64
_RECEIPT_ID = "receipt-1"
_FACT_BASELINE_ID = "assurance.quality.agent.fact-baseline.v1"
_INSPECT_ID = "assurance.quality.agent.inspect.v1"
_MATERIALIZE_ID = "assurance.quality.materialize-assessment-inputs"
_ISSUE_TRIAGE_ID = "assurance.quality.agent.issue-triage.v1"
_ISSUE_ANALYSIS_ID = "assurance.quality.agent.issue-analysis.v1"
_REPORT_ID = "assurance.quality.agent.report.v1"
_GRAPH_CONTRACT_IDS = (
    _MATERIALIZE_ID,
    _FACT_BASELINE_ID,
    _INSPECT_ID,
    _ISSUE_TRIAGE_ID,
    _ISSUE_ANALYSIS_ID,
    _REPORT_ID,
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
_GRAPHS_ROOT = Path(__file__).resolve().parents[1] / "assurance_quality" / "graphs"


def quality_contracts() -> dict[str, TaskAttemptContract[Any, Any]]:
    contracts = {
        contract.contract_id: contract.to_task_contract() for contract in AGENT_JOB_CONTRACTS.values()
    }
    contracts.update({contract.contract_id: contract for contract in TASK_ATTEMPT_CONTRACTS.values()})
    return contracts


def _skill_digests() -> dict[str, str]:
    return {
        "execution_digest": "e" * 64,
        "healing_digest": "b" * 64,
        "trace_digest": "d" * 64,
        "coverage_digest": "c" * 64,
        "metrics_digest": "a" * 64,
        "case_digest": "1" * 64,
        "plan_digest": "2" * 64,
        "mapping_digest": "3" * 64,
        "issue_digest": "4" * 64,
    }


def quality_graph_input(
    *,
    activation: dict[str, str] | None = None,
    coverage_state: str | None = None,
    classification: str | None = None,
    report_refs: list[dict[str, str]] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "change_id": "CH-DEMO-001",
        "batch_id": "20260822T000000Z",
        "capability_leafs": ["entities.item.create"],
        "allowed_artifact_paths": ["qa/changes"],
        "evidence_refs": [{"path": "qa/changes/CH-DEMO-001/execution/result.json", "digest": _SHA}],
        "execution_status": "passed",
        "budgets": {"coverage_rounds": 2, "failure_rounds": 1},
        "rounds_budget": 2,
        "rounds_used": 0,
        **_skill_digests(),
    }
    if activation is not None:
        payload["activation"] = activation
    if coverage_state is not None:
        payload["coverage_state"] = coverage_state
    if classification is not None:
        payload["classification"] = classification
    if report_refs is not None:
        payload["report_refs"] = report_refs
    return payload


def assess_graph_input(*, kind: str = "root", value: str = "1") -> dict[str, object]:
    payload = quality_graph_input(activation={"kind": kind, "value": value})
    ref = lambda path: {"path": path, "digest": _SHA}  # noqa: E731
    reviewed = {
        "change_id": "CH-DEMO-001",
        "coverage_epoch": 2,
        "preparation_refs": [ref("qa/changes/CH-DEMO-001/requirement.md")],
        "case_refs": [ref("qa/changes/CH-DEMO-001/cases/items/case.yaml")],
        "review_ref": ref("qa/changes/CH-DEMO-001/review/case-review.json"),
    }
    generation = {
        "change_id": "CH-DEMO-001",
        "coverage_epoch": 2,
        "reviewed_case": reviewed,
        "mapping_ref": ref("qa/changes/CH-DEMO-001/codegen/closed-mapping.json"),
        "source_refs": [ref("qa/changes/CH-DEMO-001/generated/api/files/tests/a.py")],
        "plan_refs": [ref("qa/changes/CH-DEMO-001/plans/api-plan.md")],
    }
    payload.update(
        {
            "coverage_epoch": 2,
            "reviewed_case": reviewed,
            "generation_result": generation,
            "execution_result": {
                "change_id": "CH-DEMO-001",
                "coverage_epoch": 2,
                "repair_round": 0,
                "batch_id": "20260822T000000Z",
                "final_status": "PASS",
                "evidence_ref": ref("qa/changes/CH-DEMO-001/execution/execute-result.json"),
                "mapping_ref": generation["mapping_ref"],
                "source_refs": generation["source_refs"],
                "receipt": {"receipt_id": "execution", "receipt_digest": _SHA},
            },
            "policy_resource_id": "assurance.product.configuration.product-policy",
            "policy_sha256": _SHA,
            "execution_at": "2026-08-22T00:00:00Z",
            "healing_ref": None,
            "issue_ref": None,
        }
    )
    return payload


def _receipt() -> ReceiptRef:
    return ReceiptRef(receipt_id=_RECEIPT_ID, receipt_digest=_SHA)


def _fact_baseline_output() -> dict[str, object]:
    baseline = {"path": "qa/changes/CH-DEMO-001/facts/fact-baseline.json", "digest": _SHA}
    return {
        "agent_result": {"source": "unavailable", "change_id": "CH-DEMO-001"},
        "assessment": _assessment_output(),
        "fact_baseline_ref": baseline,
    }


def _assessment_output() -> dict[str, object]:
    base = "qa/changes/CH-DEMO-001/inspect/epochs/2/batches/20260822T000000Z"
    return {
        "change_id": "CH-DEMO-001",
        "coverage_epoch": 2,
        "batch_id": "20260822T000000Z",
        "scope": {
            "change_id": "CH-DEMO-001",
            "coverage_epoch": 2,
            "required_case_ids": ["TC_ITEM_001"],
            "selected_families": ["api"],
            "applicable_goals": ["constraint_coverage"],
            "applicability_refs": [{"path": "qa/changes/CH-DEMO-001/requirement.md", "digest": _SHA}],
            "risk_tier": "high",
            "policy_digest": _SHA,
        },
        "policy": {
            "coverage_floor_by_tier": {
                "low": 0.7,
                "medium": 0.8,
                "high": 0.9,
                "critical": 1.0,
            }
        },
        "trace_ref": {"path": f"{base}/trace.json", "digest": _SHA},
        "gaps_ref": {"path": f"{base}/coverage-gaps.json", "digest": _SHA},
        "metrics_ref": {"path": f"{base}/metrics.json", "digest": _SHA},
        "sufficiency_ref": {"path": f"{base}/trace-sufficiency.json", "digest": _SHA},
        "execution_ref": {
            "path": "qa/changes/CH-DEMO-001/execution/execute-result.json",
            "digest": _SHA,
        },
        "healing_ref": None,
        "issue_ref": None,
    }


def _metrics_output() -> dict[str, object]:
    layers = {
        "diff_coverage": "backend",
        "constraint_coverage": "api",
        "auth_matrix_coverage": "api",
        "journey_coverage": "e2e",
        "baseline_drift": "performance",
        "mutation_score": "backend",
        "assertion_strength": "cross",
        "threshold_slack": "performance",
        "adversarial_yield": "api",
        "adversarial_clean": "cross",
    }
    metrics: dict[MetricKey, MetricEntry] = {
        key: MetricEntry(layer=cast(Any, layers[key]), status="skipped") for key in METRIC_KEYS
    }
    scope = MetricScope.of(total=1, covered=1)
    metrics["constraint_coverage"] = MetricEntry(
        layer="api",
        status="evaluated",
        value=1.0,
        declared=scope,
        evidence="trace",
    )
    return MetricsDocument.of(
        risk=BoundRisk(lower_bound="high"),
        change_id="CH-DEMO-001",
        cadence="pr",
        computed_at=datetime(2026, 8, 22, tzinfo=UTC),
        metrics=metrics,
        policy_digest=_SHA,
        floor_ratio=1.0,
    ).model_dump(mode="json")


def _sufficiency_output() -> dict[str, object]:
    return TraceSufficiencyFacts.model_validate(
        {
            "schema_version": "1",
            "change_id": "CH-DEMO-001",
            "authoritative_batch_id": "20260822T000000Z",
            "policy_digest": _SHA,
            "as_of": "2026-08-22T00:00:00Z",
            "integrity": "complete",
            "integrity_blocks_routing": False,
            "sufficient": True,
            "has_open_problems": False,
            "error_code": None,
            "insufficient_cases": [],
            "gap_codes": [],
        }
    ).model_dump(mode="json")


def _inspect_output() -> dict[str, object]:
    source = assess_graph_input()
    reviewed = cast(dict[str, object], source["reviewed_case"])
    generation = cast(dict[str, object], source["generation_result"])
    return {
        "agent_result": {
            "schema_version": "1.0",
            "change_id": "CH-DEMO-001",
            "batch_id": "20260822T000000Z",
            "inspect_mode": "primary",
            "classification_performed": True,
            "status": "no_failures",
            "execution_digest": _SHA,
            "healing_digest": None,
            "trace_digest": _SHA,
            "coverage_digest": _SHA,
            "metrics_digest": _SHA,
        },
        "assessment": _assessment_output(),
        "reviewed_case": reviewed,
        "mapping_ref": generation["mapping_ref"],
        "metrics": _metrics_output(),
        "sufficiency": _sufficiency_output(),
        "failure_facts": {
            "identity_valid": True,
            "blocking_failure": False,
            "needs_human": False,
            "repairable_failure": False,
        },
        "fact_baseline_ref": {
            "path": "qa/changes/CH-DEMO-001/facts/fact-baseline.json",
            "digest": _SHA,
        },
        "reason_codes": [],
    }


def _issue_output(*, classification: str = "test", fix_eligible: bool = True) -> dict[str, object]:
    return {
        "classification": classification,
        "fix_eligible": fix_eligible,
        "evidence_refs": [{"path": "qa/changes/CH-DEMO-001/inspect/issue-analysis.json", "digest": _SHA}],
        "rounds_budget": 2,
        "rounds_used": 0,
    }


def _report_output() -> dict[str, object]:
    return {
        "schema_version": "1.1",
        "change_id": "CH-DEMO-001",
        "batch_id": "20260822T000000Z",
        "coverage_state": "satisfied",
        "report_refs": [{"path": "qa/changes/CH-DEMO-001/report/report.md", "digest": _SHA}],
        **_skill_digests(),
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
        owner_id="assurance.quality",
        contracts=quality_contracts(),
    )


def test_quality_factory_exports_five_public_graphs(recording_context) -> None:
    bundle = build_quality_graphs(recording_context)
    assert tuple(item.name for item in fields(bundle)) == (
        "assess",
        "issue_review",
        "issue_analyze",
        "issue_reconcile",
        "report",
    )
    assert isinstance(bundle, QualityGraphs)
    assert not hasattr(bundle, "nodes")
    assert set(recording_context.bound_contract_ids) == set(_GRAPH_CONTRACT_IDS)
    assert len(set(recording_context.bound_contract_ids)) == 6
    assert recording_context.bound_contract_ids.count(_ISSUE_ANALYSIS_ID) == 2
    assert all(item is None for item in recording_context.compiled_subgraph_checkpointers)


def test_target_graphs_contain_no_phase_nodes_or_private_table(recording_context) -> None:
    bundle = build_quality_graphs(recording_context)
    names = set()
    for graph in (
        bundle.assess,
        bundle.issue_review,
        bundle.issue_analyze,
        bundle.issue_reconcile,
        bundle.report,
    ):
        names.update(_node_names(graph))
    assert names.isdisjoint(_PHASE_NODES)
    for path in _walk_graph_python():
        source = path.read_text(encoding="utf-8")
        assert "capability_slot" not in source
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                assert node.id != "Send"


def test_assess_public_input_requires_typed_activation() -> None:
    from assurance_quality.graphs.nodes import activation_assess

    with pytest.raises((ValidationError, ValueError, TypeError, KeyError)):
        activation_assess(quality_graph_input())
    initial = activation_assess(assess_graph_input(kind="root", value="1"))
    recheck = activation_assess(assess_graph_input(kind="round", value="1"))
    assert initial == BusinessActivation.one_shot()
    assert recheck == BusinessActivation.for_round(1)
    assert initial != recheck


async def test_assess_publishes_coverage_state_rounds_and_evidence() -> None:
    harness = GraphHarness()
    context = harness.recording_context(owner_id="assurance.quality", contracts=quality_contracts())
    bundle = build_quality_graphs(context)
    receipt = _receipt()
    result = await harness.run(
        bundle.assess,
        input=assess_graph_input(),
        script={
            "quality.materialize-assessment-inputs": [committed(_assessment_output(), receipt)],
            "quality.fact-baseline": [committed(_fact_baseline_output(), receipt)],
            "quality.inspect": [committed(_inspect_output(), receipt)],
        },
    )
    assert [call.semantic_node_id for call in result.semantic_calls] == [
        "quality.materialize-assessment-inputs",
        "quality.fact-baseline",
        "quality.inspect",
    ]
    assert [call.contract_id for call in result.semantic_calls] == [
        _MATERIALIZE_ID,
        _FACT_BASELINE_ID,
        _INSPECT_ID,
    ]
    published = result.published_update
    assert published is not None
    assert published["coverage_state"] == "satisfied"
    assert published["rounds_budget"] == 2
    assert published["rounds_used"] == 0
    inspection_outcome = published["inspection_outcome"]
    evidence_refs = published["evidence_refs"]
    assert isinstance(inspection_outcome, dict)
    assert isinstance(evidence_refs, list)
    assert inspection_outcome["disposition"] == "satisfied"
    receipt_payload = inspection_outcome["inspection_receipt"]
    assert isinstance(receipt_payload, dict)
    assert receipt_payload["receipt_id"] == _RECEIPT_ID
    assert len(evidence_refs) == 6
    assert result.terminal is not None


async def test_assess_stops_before_fact_baseline_when_materialization_fails() -> None:
    harness = GraphHarness()
    context = harness.recording_context(owner_id="assurance.quality", contracts=quality_contracts())
    bundle = build_quality_graphs(context)
    result = await harness.run(
        bundle.assess,
        input=assess_graph_input(),
        script={
            "quality.materialize-assessment-inputs": [
                PermanentTaskFailure(kind="invalid_input", message="digest drift")
            ]
        },
    )
    assert [call.semantic_node_id for call in result.semantic_calls] == [
        "quality.materialize-assessment-inputs"
    ]
    assert result.terminal is not None


@pytest.mark.parametrize(
    ("graph_name", "semantic_node_id", "contract_id"),
    [
        ("issue_review", "quality.issue-review", _ISSUE_TRIAGE_ID),
        ("issue_analyze", "quality.issue-analyze", _ISSUE_ANALYSIS_ID),
        ("issue_reconcile", "quality.issue-reconcile", _ISSUE_ANALYSIS_ID),
    ],
)
async def test_issue_exports_are_independently_callable(
    graph_name: str, semantic_node_id: str, contract_id: str
) -> None:
    harness = GraphHarness()
    context = harness.recording_context(owner_id="assurance.quality", contracts=quality_contracts())
    bundle = build_quality_graphs(context)
    graph = getattr(bundle, graph_name)
    result = await harness.run(
        graph,
        input=quality_graph_input(classification="test"),
        script={semantic_node_id: [committed(_issue_output(), _receipt())]},
    )
    assert [call.semantic_node_id for call in result.semantic_calls] == [semantic_node_id]
    assert [call.contract_id for call in result.semantic_calls] == [contract_id]
    published = result.published_update
    assert published is not None
    assert published["classification"] == "test"
    assert published["fix_eligible"] is True
    assert result.terminal is not None


async def test_report_rejects_coverage_flag_and_preexisting_report_references() -> None:
    harness = GraphHarness()
    context = harness.recording_context(owner_id="assurance.quality", contracts=quality_contracts())
    bundle = build_quality_graphs(context)
    report_refs = [{"path": "qa/changes/CH-DEMO-001/report/report.md", "digest": _SHA}]
    payload = quality_graph_input(coverage_state="satisfied", report_refs=report_refs)
    with pytest.raises(ValidationError):
        await harness.run(
            bundle.report,
            input=payload,
            script={"quality.report": [committed(_report_output(), _receipt())]},
        )
