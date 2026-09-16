from __future__ import annotations

import json

import pytest

from assurance_improvement.contracts.dashboard import DashboardMetric
from assurance_improvement.contracts.retro import (
    BatchMemberEvidenceGapSignal,
    ConfirmedEscapeSignal,
    DomainEvidenceGapSignal,
    EvalTrendSignal,
    GatePushbackSignal,
    HealingSignal,
    IssuePatternSignal,
    LowPromotionRateSignal,
    LowReplayStabilitySignal,
    ReopenedCoverageGapSignal,
    RetroPipelineFailureSignal,
    SkillDriftSignal,
    TaskFailureSignal,
)
from assurance_improvement.operations.retro_dashboard import build_retro_dashboard, project_signal_metrics

_REFS = {"problem_ids": ["PROB-1"], "occurrence_ids": []}


def _base(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "signal_id": "sig-1",
        "summary": "summary",
        "occurrence_count": 1,
        "recommended_change": "change it",
        "source_refs": _REFS,
        "confidence": "high",
    }
    payload.update(overrides)
    return payload


def test_issue_pattern_projection() -> None:
    signal = IssuePatternSignal.model_validate(
        _base(
            pattern_kind="knowledge_gap",
            affected_surface={"kind": "knowledge_registry", "value": ".aa/data-knowledge.yaml"},
            symptom="adapters missing",
        )
    )
    assert project_signal_metrics(signal) == (
        DashboardMetric(label="pattern_kind", value="knowledge_gap"),
        DashboardMetric(label="surface", value="knowledge_registry:.aa/data-knowledge.yaml"),
        DashboardMetric(label="symptom", value="adapters missing"),
    )


def test_task_failure_projection() -> None:
    signal = TaskFailureSignal.model_validate(
        _base(node_id="intake.case-review", error_kind="invalid_output", message_fingerprint="f" * 64)
    )
    assert project_signal_metrics(signal) == (
        DashboardMetric(label="node", value="intake.case-review"),
        DashboardMetric(label="error_kind", value="invalid_output"),
        DashboardMetric(label="fingerprint", value="f" * 12),
    )


def test_gate_pushback_projection() -> None:
    signal = GatePushbackSignal.model_validate(
        _base(gate_id="quality-gate", cause="coverage below threshold")
    )
    assert project_signal_metrics(signal) == (
        DashboardMetric(label="gate", value="quality-gate"),
        DashboardMetric(label="cause", value="coverage below threshold"),
    )


def test_healing_projection() -> None:
    signal = HealingSignal.model_validate(_base(operation="repair", outcome="succeeded"))
    assert project_signal_metrics(signal) == (
        DashboardMetric(label="operation", value="repair"),
        DashboardMetric(label="outcome", value="succeeded"),
    )


def test_skill_drift_projection() -> None:
    signal = SkillDriftSignal.model_validate(_base(phase="case-design", expected_skill="aa-case-design"))
    assert project_signal_metrics(signal) == (
        DashboardMetric(label="phase", value="case-design"),
        DashboardMetric(label="expected_skill", value="aa-case-design"),
    )


def test_eval_trend_projection() -> None:
    signal = EvalTrendSignal.model_validate(
        _base(
            source_refs={"eval_run_ids": ["run-1"]},
            suite="assurance-execution",
            verdict="failed",
            failure_signature="a" * 64,
            consecutive_count=3,
            sample_run_ids=["run-1", "run-2"],
        )
    )
    assert project_signal_metrics(signal) == (
        DashboardMetric(label="suite", value="assurance-execution"),
        DashboardMetric(label="verdict", value="failed"),
        DashboardMetric(label="consecutive", value="3"),
        DashboardMetric(label="signature", value="a" * 12),
        DashboardMetric(label="sample_runs", value="2"),
    )


def test_low_promotion_rate_projection() -> None:
    signal = LowPromotionRateSignal.model_validate(_base(rate=0.25, numerator=1, denominator=4))
    assert project_signal_metrics(signal) == (
        DashboardMetric(label="rate", value="0.2500"),
        DashboardMetric(label="ratio", value="1/4"),
    )


def test_low_replay_stability_projection() -> None:
    signal = LowReplayStabilitySignal.model_validate(_base(rate=0.6, success=3, attempts=5))
    assert project_signal_metrics(signal) == (
        DashboardMetric(label="rate", value="0.6000"),
        DashboardMetric(label="ratio", value="3/5"),
    )


def test_confirmed_escape_projection() -> None:
    signal = ConfirmedEscapeSignal.model_validate(
        _base(problem_id="PROB-1", missed_obligation_ids=["OBL-1", "OBL-2"])
    )
    assert project_signal_metrics(signal) == (
        DashboardMetric(label="problem_id", value="PROB-1"),
        DashboardMetric(label="missed_obligations", value="2"),
    )


def test_reopened_coverage_gap_projection_with_optional_fields() -> None:
    signal = ReopenedCoverageGapSignal.model_validate(
        _base(
            gap_kind="constraint_uncovered",
            locator_fingerprint="c" * 64,
            change_id="CH-1",
            case_id="CASE-1",
            cell="row=1,col=2",
        )
    )
    assert project_signal_metrics(signal) == (
        DashboardMetric(label="gap_kind", value="constraint_uncovered"),
        DashboardMetric(label="locator", value="c" * 12),
        DashboardMetric(label="change_id", value="CH-1"),
        DashboardMetric(label="case_id", value="CASE-1"),
        DashboardMetric(label="cell", value="row=1,col=2"),
    )


def test_reopened_coverage_gap_projection_omits_absent_optional_fields() -> None:
    signal = ReopenedCoverageGapSignal.model_validate(
        _base(gap_kind="constraint_uncovered", locator_fingerprint="c" * 64, change_id="CH-1")
    )
    assert project_signal_metrics(signal) == (
        DashboardMetric(label="gap_kind", value="constraint_uncovered"),
        DashboardMetric(label="locator", value="c" * 12),
        DashboardMetric(label="change_id", value="CH-1"),
    )


def test_batch_member_evidence_gap_projection() -> None:
    signal = BatchMemberEvidenceGapSignal.model_validate(
        _base(
            change_id="CH-1",
            execution_status="failed",
            domain="issue",
            reason_code="workspace_missing",
        )
    )
    assert project_signal_metrics(signal) == (
        DashboardMetric(label="change_id", value="CH-1"),
        DashboardMetric(label="execution_status", value="failed"),
        DashboardMetric(label="reason_code", value="workspace_missing"),
    )


def test_domain_evidence_gap_projection() -> None:
    signal = DomainEvidenceGapSignal.model_validate(
        _base(domain="discovery", reason_code="projection_missing")
    )
    assert project_signal_metrics(signal) == (
        DashboardMetric(label="evidence_domain", value="discovery"),
        DashboardMetric(label="reason_code", value="projection_missing"),
    )


def test_retro_pipeline_failure_projection() -> None:
    signal = RetroPipelineFailureSignal.model_validate(
        _base(failure_id="FAIL-1", stage="synthesis", error_kind="timeout")
    )
    assert project_signal_metrics(signal) == (
        DashboardMetric(label="stage", value="synthesis"),
        DashboardMetric(label="error_kind", value="timeout"),
        DashboardMetric(label="failure_id", value="FAIL-1"),
    )


_ANALYSIS_FILES = (
    "retro-eval-analysis.json",
    "retro-issue-analysis.json",
    "retro-workflow-analysis.json",
)


def _write_analysis_files(retro_dir, *, domain_status: dict[str, str] | None = None) -> None:
    domain_status = domain_status or {"eval": "ok", "issue": "ok", "workflow": "ok"}
    for domain, filename in zip(("eval", "issue", "workflow"), _ANALYSIS_FILES, strict=True):
        status = domain_status[domain]
        payload = {
            "schema_version": "3",
            "retro_id": "retro-1",
            "domain": domain,
            "analysis_status": status,
            "failure_reason": "boom" if status == "failed" else None,
            "signals": [],
            "candidates": [],
        }
        (retro_dir / filename).write_text(json.dumps(payload), encoding="utf-8")


def test_build_retro_dashboard_on_empty_change_root_returns_all_stages_false(tmp_path) -> None:
    doc = build_retro_dashboard(tmp_path)
    assert doc.stages.model_dump() == {"analyses": False, "synthesis": False, "reconcile": False}
    assert doc.change_id is None
    assert doc.retro_id is None
    assert doc.run.model_dump()["result"] is None
    assert doc.domains == ()
    assert doc.signals == ()
    assert doc.candidates == ()


def test_build_retro_dashboard_with_only_analyses_present(tmp_path) -> None:
    retro_dir = tmp_path / "qa" / "results" / "retro"
    retro_dir.mkdir(parents=True)
    _write_analysis_files(retro_dir)

    doc = build_retro_dashboard(tmp_path)
    assert doc.stages.model_dump() == {"analyses": True, "synthesis": False, "reconcile": False}
    assert doc.retro_id is None
    assert doc.domains == ()
    assert doc.signals == ()
    assert doc.candidates == ()


def test_build_retro_dashboard_analyses_false_when_one_file_missing(tmp_path) -> None:
    retro_dir = tmp_path / "qa" / "results" / "retro"
    retro_dir.mkdir(parents=True)
    _write_analysis_files(retro_dir)
    (retro_dir / "retro-workflow-analysis.json").unlink()

    doc = build_retro_dashboard(tmp_path)
    assert doc.stages.analyses is False


def test_build_retro_dashboard_reports_unreadable_context(tmp_path) -> None:
    retro_dir = tmp_path / "qa" / "results" / "retro"
    retro_dir.mkdir(parents=True)
    (retro_dir / "context.json").write_text("{not json", encoding="utf-8")

    doc = build_retro_dashboard(tmp_path)
    assert doc.stages.synthesis is False
    assert "artifact_unreadable:context.json" in doc.run.integrity_reasons


def test_build_retro_dashboard_rejects_change_id_outside_window(tmp_path) -> None:
    retro_dir = tmp_path / "qa" / "results" / "retro"
    retro_dir.mkdir(parents=True)
    context_payload = {
        "schema_version": "3",
        "retro_id": "retro-1",
        "generated_at": "2026-09-16T12:00:00+00:00",
        "dry_run": False,
        "window": {
            "selection": {"mode": "change_ids", "requested_change_ids": ["CH-1"]},
            "change_ids": ["CH-1"],
        },
        "source_manifest": {
            "issue_slice_sha256": "sha256:" + "a" * 64,
            "workflow_slice_sha256": "sha256:" + "b" * 64,
            "eval_slice_sha256": "sha256:" + "c" * 64,
        },
        "integrity": {"status": "complete", "reasons": []},
        "domain_status": {
            "issue": {"status": "ok"},
            "workflow": {"status": "ok"},
            "eval": {"status": "ok"},
        },
        "signals": {},
        "signal_count": 0,
    }
    (retro_dir / "context.json").write_text(json.dumps(context_payload), encoding="utf-8")

    with pytest.raises(ValueError, match="not part of the retro window"):
        build_retro_dashboard(tmp_path, change_id="CH-OTHER")
