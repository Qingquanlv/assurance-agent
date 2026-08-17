"""M2 Task 6: aggregate-nightly writer folds evidence; never reopens PR metrics."""

from __future__ import annotations

import json
from pathlib import Path

from assurance_agent.artifacts.canonical import canonical_json_bytes
from assurance_agent.artifacts.models.metrics import (
    MetricKey,
    MetricScope,
    MetricsDocument,
    NIGHTLY_METRICS_REL,
    PR_METRICS_REL,
)
from assurance_agent.artifacts.models.pr_metric_evidence import (
    AdversarialYieldEvidence,
    AssertionStrengthEvidence,
    AssertionStrengthSurfaceSlice,
    BaselineDriftEvidence,
    BaselineDriftScenario,
    MutationEvidence,
)
from assurance_agent.artifacts.policy import load_policy
from assurance_agent.artifacts.policy import policy_digest as compute_policy_digest
from assurance_agent.workflow.execution.evidence import atomic_write_bytes
from assurance_agent.workflow.graph.models import ExecutableTask, RuntimeContext
from assurance_agent.workflow.graph.workspace import TaskWorkspace
from assurance_agent.workflow.metrics.adversarial_yield import ADVERSARIAL_YIELD_EVIDENCE_REL
from assurance_agent.workflow.metrics.assertion_strength import ASSERTION_STRENGTH_EVIDENCE_REL
from assurance_agent.workflow.metrics.baseline_drift import BASELINE_DRIFT_EVIDENCE_REL
from assurance_agent.workflow.metrics.mutation import MUTATION_EVIDENCE_REL
from assurance_agent.workflow.metrics.nightly import (
    NIGHTLY_SHORTBOARDS_REL,
    aggregate_nightly_metrics_operation,
    evaluate_retrospective_shortboards_operation,
    load_latest_pr_metrics_operation,
)
from tests.helpers_aa import write_aa_config

CHANGE_ID = "CH-NIGHTLY-WRITER-001"
PR_BYTES = b'{"planted":"pr-metrics-must-not-change","cadence":"pr"}\n'


def _workspace(project_root: Path) -> TaskWorkspace:
    change_dir = project_root / "qa" / "changes" / CHANGE_ID
    change_dir.mkdir(parents=True, exist_ok=True)
    return TaskWorkspace(
        task_id="t-agg-nightly",
        root=project_root,
        project_root=project_root,
        repo_root=project_root,
        change_dir=change_dir,
        base_tree_id="tree-0",
    )


def _task(target: str) -> ExecutableTask:
    return ExecutableTask.model_construct(
        task_id="t-agg-nightly",
        node_id=target.removeprefix("operation:"),
        graph_id="metrics-nightly-workflow",
        target=target,
        input={"with": {}},
    )


def _context(project_root: Path) -> RuntimeContext:
    return RuntimeContext.model_construct(
        project_root=project_root,
        repo_root=project_root,
        change_dir=project_root / "qa" / "changes" / CHANGE_ID,
        change_id=CHANGE_ID,
        params={},
    )


def _write_evidence(change_dir: Path) -> None:
    mutation = MutationEvidence(
        schema_version="1",
        change_id=CHANGE_ID,
        batch_id="nightly",
        status="evaluated",
        value=1.0,
        killed=2,
        survived=0,
        equivalent=0,
        tested=2,
        selected=2,
        budget_seconds=300,
        elapsed_seconds=1.0,
    )
    api = AssertionStrengthSurfaceSlice(
        layer="api",
        declared=MetricScope.of(total=2, covered=2),
        value=1.0,
        strong=2,
        weak=0,
    )
    e2e = AssertionStrengthSurfaceSlice(
        layer="e2e",
        declared=MetricScope.of(total=2, covered=1),
        value=0.5,
        strong=1,
        weak=1,
    )
    assertion = AssertionStrengthEvidence(
        schema_version="1",
        change_id=CHANGE_ID,
        batch_id="nightly",
        status="evaluated",
        value=0.75,
        declared=MetricScope.of(total=4, covered=3),
        surfaces=(api, e2e),
    )
    baseline = BaselineDriftEvidence(
        schema_version="1",
        change_id=CHANGE_ID,
        batch_id="nightly",
        status="evaluated",
        value=0.05,
        drift_band=0.2,
        window=5,
        scenarios=(
            BaselineDriftScenario(
                capability="list",
                endpoint="/api/items",
                current_p95_ms=105.0,
                baseline_p95_ms=100.0,
                current_error_rate=0.0,
                baseline_error_rate=0.0,
                drift=0.05,
                sample_count=4,
            ),
        ),
        shortboards=(),
    )
    for rel, model in (
        (MUTATION_EVIDENCE_REL, mutation),
        (ASSERTION_STRENGTH_EVIDENCE_REL, assertion),
        (BASELINE_DRIFT_EVIDENCE_REL, baseline),
    ):
        path = change_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(path, canonical_json_bytes(model))


def test_aggregate_writes_nightly_document_and_leaves_pr_untouched(tmp_path: Path) -> None:
    project_root = tmp_path / "proj"
    write_aa_config(project_root)
    workspace = _workspace(project_root)
    context = _context(project_root)
    pr_path = workspace.change_dir / PR_METRICS_REL
    pr_path.parent.mkdir(parents=True, exist_ok=True)
    pr_path.write_bytes(PR_BYTES)
    _write_evidence(workspace.change_dir)

    load = load_latest_pr_metrics_operation(_task("operation:load-latest-pr-metrics"), workspace, context)
    assert load.status == "succeeded"

    result = aggregate_nightly_metrics_operation(
        _task("operation:aggregate-nightly-metrics"), workspace, context
    )
    assert result.status == "succeeded"
    value = result.value
    assert isinstance(value, dict)
    assert value.get("stub") is not True
    assert pr_path.read_bytes() == PR_BYTES

    nightly_path = workspace.change_dir / NIGHTLY_METRICS_REL
    doc = MetricsDocument.model_validate(json.loads(nightly_path.read_text(encoding="utf-8")))
    assert doc.cadence == "nightly"
    assert doc.metrics["mutation_score"].status == "evaluated"
    assert doc.metrics["assertion_strength"].status == "evaluated"
    assert doc.metrics["baseline_drift"].status == "evaluated"
    # No yield file → pending (never invented collection gap).
    assert doc.metrics["adversarial_yield"].status == "not_evaluated"
    assert "confidence" not in json.loads(nightly_path.read_text(encoding="utf-8"))


def test_aggregate_folds_adversarial_yield_when_evidence_present(tmp_path: Path) -> None:
    project_root = tmp_path / "proj"
    write_aa_config(project_root)
    workspace = _workspace(project_root)
    context = _context(project_root)
    _write_evidence(workspace.change_dir)
    yield_evidence = AdversarialYieldEvidence(
        schema_version="1",
        change_id=CHANGE_ID,
        batch_id="nightly",
        property="api",
        layer="api",
        sample_count=2,
        counterexample_ids=("CE-1",),
        unclosed_count=1,
        seed=9,
        status="evaluated",
        value=1.0,
    )
    path = workspace.change_dir / ADVERSARIAL_YIELD_EVIDENCE_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(path, canonical_json_bytes(yield_evidence))

    result = aggregate_nightly_metrics_operation(
        _task("operation:aggregate-nightly-metrics"), workspace, context
    )
    assert result.status == "succeeded"
    doc = MetricsDocument.model_validate(
        json.loads((workspace.change_dir / NIGHTLY_METRICS_REL).read_text(encoding="utf-8"))
    )
    assert doc.metrics["adversarial_yield"].status == "evaluated"
    assert doc.metrics["adversarial_yield"].value == 1.0
    assert doc.metrics["adversarial_yield"].evidence == "adversarial-yield.json"
    assert not any(b.code == "pending_nightly" and b.metric == "adversarial_yield" for b in doc.shortboards)
    assert doc.metrics["adversarial_clean"].status == "evaluated"
    assert doc.metrics["adversarial_clean"].holds is False
    assert any(b.code == "adversarial_open" and b.metric == "adversarial_clean" for b in doc.shortboards)


def test_retrospective_shortboards_expose_boards_without_reopening_pr(tmp_path: Path) -> None:
    project_root = tmp_path / "proj"
    write_aa_config(project_root)
    workspace = _workspace(project_root)
    context = _context(project_root)
    pr_path = workspace.change_dir / PR_METRICS_REL
    pr_path.parent.mkdir(parents=True, exist_ok=True)
    pr_path.write_bytes(PR_BYTES)
    _write_evidence(workspace.change_dir)

    assert (
        aggregate_nightly_metrics_operation(
            _task("operation:aggregate-nightly-metrics"), workspace, context
        ).status
        == "succeeded"
    )
    result = evaluate_retrospective_shortboards_operation(
        _task("operation:evaluate-retrospective-shortboards"), workspace, context
    )
    assert result.status == "succeeded"
    value = result.value
    assert isinstance(value, dict)
    assert value["reopens_pr_verdict"] is False
    assert pr_path.read_bytes() == PR_BYTES

    payload = json.loads((workspace.change_dir / NIGHTLY_SHORTBOARDS_REL).read_text(encoding="utf-8"))
    assert payload["reopens_pr_verdict"] is False
    assert payload["source_rel"] == NIGHTLY_METRICS_REL
    assert isinstance(payload["shortboards"], list)
    assert any(board.get("code") == "pending_nightly" for board in payload["shortboards"])
    assert "confidence" not in payload
    assert "sufficiency_verdict" in payload
    # Consumers (next PR batch / retro) get shortboards; PR metrics bytes stay fixed.
    assert pr_path.read_bytes() == PR_BYTES


def test_retrospective_sufficiency_on_open_counterexamples_does_not_touch_quality_gate(
    tmp_path: Path,
) -> None:
    """Nightly hard-rule path writes shortboards only — never quality-gate / manifest."""
    project_root = tmp_path / "proj"
    write_aa_config(project_root)
    workspace = _workspace(project_root)
    context = _context(project_root)
    _write_evidence(workspace.change_dir)
    yield_evidence = AdversarialYieldEvidence(
        schema_version="1",
        change_id=CHANGE_ID,
        batch_id="nightly",
        property="api",
        layer="api",
        sample_count=1,
        counterexample_ids=("CE-open",),
        unclosed_count=1,
        seed=3,
        status="evaluated",
        value=1.0,
    )
    path = workspace.change_dir / ADVERSARIAL_YIELD_EVIDENCE_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(path, canonical_json_bytes(yield_evidence))

    qg_rel = "inspect/quality-gate-result.json"
    manifest_rel = "execution/execution-manifest.json"
    qg_path = workspace.change_dir / qg_rel
    manifest_path = workspace.change_dir / manifest_rel
    qg_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    qg_bytes = b'{"final_status":"PASS","planted":true}\n'
    manifest_bytes = b"final_status: PASS\nplanted: true\n"
    qg_path.write_bytes(qg_bytes)
    manifest_path.write_bytes(manifest_bytes)

    assert (
        aggregate_nightly_metrics_operation(
            _task("operation:aggregate-nightly-metrics"), workspace, context
        ).status
        == "succeeded"
    )
    result = evaluate_retrospective_shortboards_operation(
        _task("operation:evaluate-retrospective-shortboards"), workspace, context
    )
    assert result.status == "succeeded"
    value = result.value
    assert isinstance(value, dict)
    # Default risk tier is low → open CE routes needs_human, not stop.
    assert value["sufficiency_verdict"] == "needs_human"
    assert value["reopens_pr_verdict"] is False

    payload = json.loads((workspace.change_dir / NIGHTLY_SHORTBOARDS_REL).read_text(encoding="utf-8"))
    assert payload["sufficiency_verdict"] == "needs_human"
    assert payload["reopens_pr_verdict"] is False
    assert qg_path.read_bytes() == qg_bytes
    assert manifest_path.read_bytes() == manifest_bytes


def test_aggregate_uses_policy_digest_and_does_not_rewrite_pr_metrics_model(tmp_path: Path) -> None:
    project_root = tmp_path / "proj"
    write_aa_config(project_root)
    workspace = _workspace(project_root)
    context = _context(project_root)
    _write_evidence(workspace.change_dir)

    result = aggregate_nightly_metrics_operation(
        _task("operation:aggregate-nightly-metrics"), workspace, context
    )
    assert result.status == "succeeded"
    doc = MetricsDocument.model_validate(
        json.loads((workspace.change_dir / NIGHTLY_METRICS_REL).read_text(encoding="utf-8"))
    )
    assert doc.policy_digest == compute_policy_digest(load_policy(project_root))
    assert not (workspace.change_dir / PR_METRICS_REL).exists()


def test_naming_guard_forbids_confidence_as_floor_ratio_on_nightly_artifacts(tmp_path: Path) -> None:
    project_root = tmp_path / "proj"
    write_aa_config(project_root)
    workspace = _workspace(project_root)
    context = _context(project_root)
    _write_evidence(workspace.change_dir)
    aggregate_nightly_metrics_operation(_task("operation:aggregate-nightly-metrics"), workspace, context)
    evaluate_retrospective_shortboards_operation(
        _task("operation:evaluate-retrospective-shortboards"), workspace, context
    )
    for rel in (NIGHTLY_METRICS_REL, NIGHTLY_SHORTBOARDS_REL):
        raw = (workspace.change_dir / rel).read_text(encoding="utf-8")
        assert '"confidence"' not in raw
        if rel == NIGHTLY_METRICS_REL:
            assert '"floor_ratio"' in raw


def test_present_but_corrupt_nightly_evidence_is_artifact_corrupt_not_pending(
    tmp_path: Path,
) -> None:
    """Present-but-unparseable evidence must fail-close, not cadence-skip."""
    project_root = tmp_path / "proj"
    write_aa_config(project_root)
    workspace = _workspace(project_root)
    context = _context(project_root)

    corrupt_paths: tuple[tuple[str, MetricKey], ...] = (
        (MUTATION_EVIDENCE_REL, "mutation_score"),
        (ASSERTION_STRENGTH_EVIDENCE_REL, "assertion_strength"),
        (BASELINE_DRIFT_EVIDENCE_REL, "baseline_drift"),
    )
    for rel, _metric in corrupt_paths:
        path = workspace.change_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not-json", encoding="utf-8")

    result = aggregate_nightly_metrics_operation(
        _task("operation:aggregate-nightly-metrics"), workspace, context
    )
    assert result.status == "succeeded"
    doc = MetricsDocument.model_validate(
        json.loads((workspace.change_dir / NIGHTLY_METRICS_REL).read_text(encoding="utf-8"))
    )

    for _rel, metric in corrupt_paths:
        assert doc.metrics[metric].status == "collection_failed"
        gaps = [g for g in doc.collection_gaps if g.metric == metric]
        assert gaps
        assert gaps[0].code == "artifact_corrupt"
        assert "missing" not in gaps[0].detail.lower()
        assert not any(
            board.code == "pending_nightly" and board.metric == metric for board in doc.shortboards
        )
