from __future__ import annotations

import json
from pathlib import Path

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


def _write_analysis_files(
    retro_dir, *, domain_status: dict[str, str] | None = None, retro_id: str = "retro-1"
) -> None:
    domain_status = domain_status or {"eval": "ok", "issue": "ok", "workflow": "ok"}
    for domain, filename in zip(("eval", "issue", "workflow"), _ANALYSIS_FILES, strict=True):
        status = domain_status[domain]
        payload = {
            "schema_version": "3",
            "retro_id": retro_id,
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
    assert doc.run.integrity_reasons == ()


def test_build_retro_dashboard_with_only_analyses_present(tmp_path) -> None:
    retro_dir = tmp_path / "qa" / "results" / "retro"
    retro_dir.mkdir(parents=True)
    _write_analysis_files(retro_dir)

    doc = build_retro_dashboard(tmp_path)
    assert doc.stages.model_dump() == {"analyses": True, "synthesis": False, "reconcile": False}
    assert doc.retro_id is None
    assert doc.signals == ()
    assert doc.candidates == ()
    assert {domain.domain: domain.status for domain in doc.domains} == {
        "issue": "ok",
        "workflow": "ok",
        "eval": "ok",
        "discovery": "absent",
        "coverage_gap": "absent",
    }


def test_build_retro_dashboard_analyses_false_when_one_file_missing(tmp_path) -> None:
    retro_dir = tmp_path / "qa" / "results" / "retro"
    retro_dir.mkdir(parents=True)
    _write_analysis_files(retro_dir)
    (retro_dir / "retro-workflow-analysis.json").unlink()

    doc = build_retro_dashboard(tmp_path)
    assert doc.stages.analyses is False
    assert "artifact_missing:retro-workflow-analysis.json" in doc.run.integrity_reasons


def test_build_retro_dashboard_eval_only_projects_signals_without_context(tmp_path) -> None:
    """A mid-run retro (only eval analysis on disk) must still surface its signals.

    stages.analyses stays False until all three analysis files exist, but the
    loaded eval signals and missing-file reasons must not be discarded — otherwise
    the dashboard is indistinguishable from 'no retro run'.
    """
    retro_dir = tmp_path / "qa" / "results" / "retro"
    retro_dir.mkdir(parents=True)
    payload = {
        "schema_version": "3",
        "retro_id": "retro-eval-only",
        "domain": "eval",
        "analysis_status": "ok",
        "failure_reason": None,
        "candidates": [],
        "signals": [
            {
                "signal_id": "eval-trend-1",
                "signal_type": "eval_trend",
                "summary": "suite failed once",
                "occurrence_count": 1,
                "recommended_change": "re-run the suite",
                "source_refs": {"eval_run_ids": ["run-1"], "problem_ids": [], "occurrence_ids": []},
                "confidence": "high",
                "suite": "assurance-execution",
                "verdict": "failed",
                "failure_signature": "a" * 64,
                "consecutive_count": 0,
                "sample_run_ids": ["run-1"],
                "source_change_ids": ["CH-1"],
            }
        ],
    }
    (retro_dir / "retro-eval-analysis.json").write_text(json.dumps(payload), encoding="utf-8")

    doc = build_retro_dashboard(tmp_path, change_id="CH-1")
    assert doc.stages.analyses is False
    assert doc.stages.synthesis is False
    assert doc.change_id == "CH-1"
    assert "artifact_missing:retro-issue-analysis.json" in doc.run.integrity_reasons
    assert "artifact_missing:retro-workflow-analysis.json" in doc.run.integrity_reasons
    assert [signal.signal_id for signal in doc.signals] == ["eval-trend-1"]
    assert doc.signals[0].domain == "eval"
    eval_domain = next(domain for domain in doc.domains if domain.domain == "eval")
    assert eval_domain.status == "ok"
    assert eval_domain.signal_count == 1


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


def _full_context_payload(*, extra_signal: dict[str, object] | None = None) -> dict[str, object]:
    workflow_signals = [
        {
            "signal_id": "task-failure-1",
            "signal_type": "task_failure",
            "summary": "1 technical failure(s) at intake.case-review",
            "occurrence_count": 1,
            "recommended_change": "Review the node contract.",
            "source_refs": {"workflow_evidence_ids": ["attempt-failure-1"]},
            "confidence": "high",
            "node_id": "intake.case-review",
            "error_kind": "invalid_output",
            "message_fingerprint": "f" * 64,
        }
    ]
    if extra_signal is not None:
        workflow_signals.append(extra_signal)
    return {
        "schema_version": "3",
        "retro_id": "retro-1",
        "generated_at": "2026-09-16T12:19:50+00:00",
        "dry_run": False,
        "window": {
            "selection": {"mode": "change_ids", "requested_change_ids": ["CH-1"]},
            "change_ids": ["CH-1"],
        },
        "source_manifest": {
            "issue_slice_sha256": "sha256:" + "a" * 64,
            "workflow_slice_sha256": "sha256:" + "b" * 64,
            "eval_slice_sha256": "sha256:" + "c" * 64,
            "workflow_sources": [
                {"kind": "loop_round_history", "sha256": "d" * 64, "evidence_ids": ["loop-1"]},
                {"kind": "workflow_ledger", "sha256": "e" * 64, "evidence_ids": ["attempt-failure-1"]},
            ],
        },
        "integrity": {"status": "incomplete", "reasons": ["issue_evidence_absent"]},
        "domain_status": {
            "issue": {"status": "failed", "failure_reason": "no evidence"},
            "workflow": {"status": "ok"},
            "eval": {"status": "ok"},
        },
        "signals": {"issue": [], "workflow": workflow_signals, "eval": []},
        "signal_count": len(workflow_signals),
    }


def test_build_retro_dashboard_full_run_populates_domains_and_signals(tmp_path) -> None:
    retro_dir = tmp_path / "qa" / "results" / "retro"
    retro_dir.mkdir(parents=True)
    _write_analysis_files(retro_dir)
    (retro_dir / "context.json").write_text(json.dumps(_full_context_payload()), encoding="utf-8")

    doc = build_retro_dashboard(tmp_path)

    assert [domain.domain for domain in doc.domains] == [
        "issue",
        "workflow",
        "eval",
        "discovery",
        "coverage_gap",
    ]
    issue_domain = doc.domains[0]
    assert issue_domain.status == "failed"
    assert issue_domain.failure_reason == "no evidence"
    assert issue_domain.signal_count == 0
    assert issue_domain.source_count == 0

    workflow_domain = doc.domains[1]
    assert workflow_domain.status == "ok"
    assert workflow_domain.signal_count == 1
    assert workflow_domain.source_count == 2
    assert workflow_domain.source_kinds == {"loop_round_history": 1, "workflow_ledger": 1}

    discovery_domain = doc.domains[3]
    assert discovery_domain.status == "absent"
    assert discovery_domain.signal_count is None
    assert discovery_domain.source_count == 0

    assert len(doc.signals) == 1
    signal = doc.signals[0]
    assert signal.signal_id == "task-failure-1"
    assert signal.domain == "workflow"
    assert signal.metrics == (
        DashboardMetric(label="node", value="intake.case-review"),
        DashboardMetric(label="error_kind", value="invalid_output"),
        DashboardMetric(label="fingerprint", value="f" * 12),
    )
    assert signal.source_refs.workflow_evidence_ids == ("attempt-failure-1",)


def test_build_retro_dashboard_signals_sort_by_occurrence_desc_then_id(tmp_path) -> None:
    retro_dir = tmp_path / "qa" / "results" / "retro"
    retro_dir.mkdir(parents=True)
    _write_analysis_files(retro_dir)
    extra = {
        "signal_id": "healing-1",
        "signal_type": "healing",
        "summary": "healed",
        "occurrence_count": 5,
        "recommended_change": "n/a",
        "source_refs": {"workflow_evidence_ids": ["attempt-failure-2"]},
        "confidence": "medium",
        "operation": "repair",
        "outcome": "succeeded",
    }
    (retro_dir / "context.json").write_text(
        json.dumps(_full_context_payload(extra_signal=extra)), encoding="utf-8"
    )

    doc = build_retro_dashboard(tmp_path)
    assert [signal.signal_id for signal in doc.signals] == ["healing-1", "task-failure-1"]


def test_build_retro_dashboard_unreadable_context_with_unknown_signal_type(tmp_path) -> None:
    retro_dir = tmp_path / "qa" / "results" / "retro"
    retro_dir.mkdir(parents=True)
    _write_analysis_files(retro_dir)
    payload = _full_context_payload()
    payload["signals"]["workflow"][0] = {  # type: ignore[index]
        **payload["signals"]["workflow"][0],  # type: ignore[index]
        "signal_type": "future_signal_type",
    }
    (retro_dir / "context.json").write_text(json.dumps(payload), encoding="utf-8")

    doc = build_retro_dashboard(tmp_path)
    assert doc.stages.synthesis is False
    assert "artifact_unreadable:context.json" in doc.run.integrity_reasons


class _FakeSignal:
    signal_type = "future_signal_type"


def test_project_signal_metrics_returns_empty_tuple_for_unknown_type() -> None:
    from assurance_improvement.operations.retro_dashboard import project_signal_metrics

    assert project_signal_metrics(_FakeSignal()) == ()  # type: ignore[arg-type]


_REAL_CONTEXT = {
    "schema_version": "3",
    "retro_id": "retro-a8e10021fee6abf1ec44671cb8d8d9b38d613722e6df2946c1164cda30071b39",
    "generated_at": "2026-09-16T12:19:50.482167+00:00",
    "dry_run": False,
    "window": {
        "selection": {
            "mode": "change_ids",
            "requested_change_ids": ["BENCH-opencode-ret-dept-management-20260916-113031-330191e8"],
        },
        "change_ids": ["BENCH-opencode-ret-dept-management-20260916-113031-330191e8"],
    },
    "source_manifest": {
        "issue_slice_sha256": "sha256:" + "1" * 64,
        "workflow_slice_sha256": "sha256:" + "2" * 64,
        "eval_slice_sha256": "sha256:" + "3" * 64,
        "workflow_sources": [
            {"kind": "loop_round_history", "sha256": "4" * 64, "evidence_ids": ["loop-1"]},
            {
                "kind": "workflow_ledger",
                "sha256": "5" * 64,
                "evidence_ids": [
                    "attempt-failure-43ddc93ea1d423fdddd032ebab4d17c2f530230c535a5c488036a8d40d1cc761"
                ],
            },
        ],
        "eval_sources": [
            {"kind": "report_outcome", "sha256": "6" * 64, "evidence_ids": []},
            {
                "kind": "eval_run",
                "sha256": "7" * 64,
                "evidence_ids": ["847d09aa43e53799011f682eea02b821263135b6926ab59716aee5370affa79f"],
            },
        ],
    },
    "integrity": {
        "status": "incomplete",
        "reasons": ["issue_signal_analysis_failed", "issue_evidence_absent", "skill_drift_evidence_absent"],
    },
    "domain_status": {
        "issue": {
            "status": "failed",
            "failure_reason": "Authenticated issue evidence slice is empty",
        },
        "workflow": {"status": "ok"},
        "eval": {"status": "ok"},
    },
    "signals": {
        "issue": [],
        "workflow": [
            {
                "signal_id": "task-failure-58deee4f67475fc5914c83eb991bee49634ef76fd08c2538af81722922fbca00",
                "signal_type": "task_failure",
                "summary": "1 technical failure(s) at intake.case-review (invalid_output); recovery does not erase failures.",
                "occurrence_count": 1,
                "recommended_change": "Review the node's contract and failure fingerprint before proposing a targeted correction.",
                "source_refs": {
                    "workflow_evidence_ids": [
                        "attempt-failure-43ddc93ea1d423fdddd032ebab4d17c2f530230c535a5c488036a8d40d1cc761"
                    ]
                },
                "confidence": "high",
                "node_id": "intake.case-review",
                "error_kind": "invalid_output",
                "message_fingerprint": "20ff93741b102bf446620dd8e36c3c5f04de2296dbf4625f9fe89ee84ec34c8e",
            }
        ],
        "eval": [
            {
                "signal_id": "eval-trend-67832d289e74aa4a6d3daaa486f557e06693fe510325b2474c58b988305ac158",
                "signal_type": "eval_trend",
                "summary": "Failure observation in suite 'assurance-execution'.",
                "occurrence_count": 1,
                "recommended_change": "Triage the failing dept API sample group.",
                "source_refs": {
                    "eval_run_ids": ["847d09aa43e53799011f682eea02b821263135b6926ab59716aee5370affa79f"]
                },
                "confidence": "medium",
                "suite": "assurance-execution",
                "verdict": "failed",
                "failure_signature": "67832d289e74aa4a6d3daaa486f557e06693fe510325b2474c58b988305ac158",
                "consecutive_count": 0,
                "sample_run_ids": ["847d09aa43e53799011f682eea02b821263135b6926ab59716aee5370affa79f"],
            }
        ],
    },
    "signal_count": 2,
}

_REAL_CANDIDATES = {
    "schema_version": "3",
    "retro_id": _REAL_CONTEXT["retro_id"],
    "context_sha256": "sha256:" + "8" * 64,
    "candidates": [
        {
            "candidate_id": "cand-workflow-intake-case-review-contract-hardening",
            "kind": "workflow_improvement",
            "delivery": "change_draft",
            "knowledge_delta": None,
            "source_refs": {
                "workflow_evidence_ids": [
                    "attempt-failure-43ddc93ea1d423fdddd032ebab4d17c2f530230c535a5c488036a8d40d1cc761"
                ]
            },
            "target": "intake.case-review",
            "rationale": "The authenticated workflow ledger records one technical failure.",
            "proposed_change": "Harden the intake.case-review node contract.",
            "risk": "medium",
            "confidence": "high",
            "signal_ids": ["task-failure-58deee4f67475fc5914c83eb991bee49634ef76fd08c2538af81722922fbca00"],
            "verification": {
                "suites": ["assurance-execution"],
                "required_cases": ["intake.case-review accepts contract-valid output"],
                "success_criteria": "Re-running intake.case-review yields contract-valid output.",
            },
            "supersedes": None,
        },
        {
            "candidate_id": "cand-test-dept-api-triage-67832d28",
            "kind": "test_improvement",
            "delivery": "change_draft",
            "knowledge_delta": None,
            "source_refs": {
                "eval_run_ids": ["847d09aa43e53799011f682eea02b821263135b6926ab59716aee5370affa79f"]
            },
            "target": "suite 'assurance-execution' dept API sample group",
            "rationale": "One authenticated eval run returned verdict 'failed'.",
            "proposed_change": "Add a deterministic dept API triage/regression case.",
            "risk": "low",
            "confidence": "medium",
            "signal_ids": ["eval-trend-67832d289e74aa4a6d3daaa486f557e06693fe510325b2474c58b988305ac158"],
            "verification": {
                "suites": ["assurance-execution"],
                "required_cases": ["dept API dept-management sample group triage run"],
                "success_criteria": "Repeated runs report a deterministic verdict.",
            },
            "supersedes": None,
        },
    ],
}

_REAL_STATUS = {
    "schema_version": "1",
    "retro_id": _REAL_CONTEXT["retro_id"],
    "batch_id": None,
    "result": "completed_with_gaps",
    "improvement_ids": ["IMP-D676E2ECFC80E7CBEA78", "IMP-58CB93E8BAEF3FBAE12C"],
    "outbox_id": None,
    "failure_ids": [],
}

_REAL_LEDGER = {
    "schema_version": "1",
    "last_seq": 2,
    "by_fingerprint": {},
    "improvements": {
        "IMP-D676E2ECFC80E7CBEA78": {
            "improvement_id": "IMP-D676E2ECFC80E7CBEA78",
            "fingerprint": "d" * 64,
            "kind": "workflow_improvement",
            "delivery": "change_draft",
            "source_refs": {
                "workflow_evidence_ids": [
                    "attempt-failure-43ddc93ea1d423fdddd032ebab4d17c2f530230c535a5c488036a8d40d1cc761"
                ]
            },
            "target": "intake.case-review",
            "rationale": "…",
            "proposed_change": "…",
            "verification": {"success_criteria": "…"},
            "risk": "medium",
            "confidence": "high",
            "state": "proposed",
            "version": 1,
            "proposed_by_retro_ids": [_REAL_CONTEXT["retro_id"]],
            "last_event_id": "IMPEVT-1",
        },
        "IMP-58CB93E8BAEF3FBAE12C": {
            "improvement_id": "IMP-58CB93E8BAEF3FBAE12C",
            "fingerprint": "e" * 64,
            "kind": "test_improvement",
            "delivery": "change_draft",
            "source_refs": {
                "eval_run_ids": ["847d09aa43e53799011f682eea02b821263135b6926ab59716aee5370affa79f"]
            },
            "target": "suite 'assurance-execution' dept API sample group",
            "rationale": "…",
            "proposed_change": "…",
            "verification": {"success_criteria": "…"},
            "risk": "low",
            "confidence": "medium",
            "state": "proposed",
            "version": 1,
            "proposed_by_retro_ids": [_REAL_CONTEXT["retro_id"]],
            "last_event_id": "IMPEVT-2",
        },
    },
}


def _write_full_run(tmp_path) -> Path:
    retro_dir = tmp_path / "qa" / "results" / "retro"
    retro_dir.mkdir(parents=True)
    # retro_id must match _REAL_CONTEXT's retro_id so the stale-run
    # (retro_id mismatch) integrity check introduced in Fix 2 does not
    # spuriously fire against this otherwise-consistent fixture.
    _write_analysis_files(retro_dir, retro_id=_REAL_CONTEXT["retro_id"])  # type: ignore[arg-type]
    (retro_dir / "context.json").write_text(json.dumps(_REAL_CONTEXT), encoding="utf-8")
    (retro_dir / "candidates.json").write_text(json.dumps(_REAL_CANDIDATES), encoding="utf-8")
    (retro_dir / "status.json").write_text(json.dumps(_REAL_STATUS), encoding="utf-8")
    ledger_dir = tmp_path / "qa" / "improvements"
    ledger_dir.mkdir(parents=True)
    (ledger_dir / "ledger.json").write_text(json.dumps(_REAL_LEDGER), encoding="utf-8")
    return tmp_path


def test_build_retro_dashboard_full_run_joins_candidates_to_ledger(tmp_path) -> None:
    root = _write_full_run(tmp_path)
    doc = build_retro_dashboard(root)

    assert doc.stages.model_dump() == {"analyses": True, "synthesis": True, "reconcile": True}
    assert doc.run.result == "completed_with_gaps"
    assert doc.run.candidate_count == 2
    assert doc.run.improvement_ids == ("IMP-D676E2ECFC80E7CBEA78", "IMP-58CB93E8BAEF3FBAE12C")
    assert "issue_signal_analysis_failed" in doc.run.integrity_reasons

    assert len(doc.candidates) == 2
    first = next(
        c for c in doc.candidates if c.candidate_id == "cand-workflow-intake-case-review-contract-hardening"
    )
    assert first.improvement_id == "IMP-D676E2ECFC80E7CBEA78"
    assert first.improvement_state == "proposed"
    assert first.improvement_version == 1
    assert first.has_knowledge_delta is False

    workflow_signal = next(s for s in doc.signals if s.domain == "workflow")
    assert workflow_signal.cited_by_candidate_ids == ("cand-workflow-intake-case-review-contract-hardening",)


def test_build_retro_dashboard_missing_ledger_leaves_improvement_fields_null(tmp_path) -> None:
    root = _write_full_run(tmp_path)
    (root / "qa" / "improvements" / "ledger.json").unlink()

    doc = build_retro_dashboard(root)
    assert all(c.improvement_id is None for c in doc.candidates)
    assert all(c.improvement_state is None for c in doc.candidates)
    assert all(c.improvement_version is None for c in doc.candidates)


def test_build_retro_dashboard_ledger_join_mismatch_is_flagged(tmp_path) -> None:
    root = _write_full_run(tmp_path)
    ledger = json.loads((root / "qa" / "improvements" / "ledger.json").read_text(encoding="utf-8"))
    ledger["improvements"]["IMP-D676E2ECFC80E7CBEA78"]["target"] = "a different target"
    (root / "qa" / "improvements" / "ledger.json").write_text(json.dumps(ledger), encoding="utf-8")

    doc = build_retro_dashboard(root)
    mismatched = next(
        c for c in doc.candidates if c.candidate_id == "cand-workflow-intake-case-review-contract-hardening"
    )
    assert mismatched.improvement_id is None
    assert mismatched.improvement_state is None
    assert "improvement_join_mismatch" in doc.run.integrity_reasons
    other = next(c for c in doc.candidates if c.candidate_id == "cand-test-dept-api-triage-67832d28")
    assert other.improvement_id == "IMP-58CB93E8BAEF3FBAE12C"


def test_build_retro_dashboard_reconcile_not_run_leaves_improvement_fields_null(tmp_path) -> None:
    root = _write_full_run(tmp_path)
    (root / "qa" / "results" / "retro" / "status.json").unlink()

    doc = build_retro_dashboard(root)
    assert doc.stages.reconcile is False
    assert doc.run.result is None
    assert doc.run.improvement_ids == ()
    assert all(c.improvement_id is None for c in doc.candidates)


def test_build_retro_dashboard_missing_context_still_populates_run_and_candidates(tmp_path) -> None:
    """Fix 1: a corrupt/missing context.json must not discard run_status or candidates.

    Only genuinely context-derived DashboardRun fields (integrity_status,
    signal_count, window_change_ids, selection_mode) should fall back to their
    model defaults when context is None; result/batch_id/failure_ids/
    improvement_ids/candidate_count come from run_status/candidates
    regardless of context.
    """
    root = _write_full_run(tmp_path)
    (root / "qa" / "results" / "retro" / "context.json").unlink()

    doc = build_retro_dashboard(root)
    assert doc.stages.synthesis is False
    assert doc.retro_id is None

    # Context-derived fields fall back to DashboardRun defaults.
    assert doc.run.integrity_status is None
    assert doc.run.signal_count == 0
    assert doc.run.window_change_ids == ()
    assert doc.run.selection_mode is None

    # run_status- and candidates-derived fields must NOT be discarded.
    assert doc.run.result == "completed_with_gaps"
    assert doc.run.batch_id is None
    assert doc.run.failure_ids == ()
    assert doc.run.improvement_ids == ("IMP-D676E2ECFC80E7CBEA78", "IMP-58CB93E8BAEF3FBAE12C")
    assert doc.run.candidate_count == 2

    assert len(doc.candidates) == 2
    first = next(
        c for c in doc.candidates if c.candidate_id == "cand-workflow-intake-case-review-contract-hardening"
    )
    assert first.improvement_id == "IMP-D676E2ECFC80E7CBEA78"
    assert first.improvement_state == "proposed"
    assert first.improvement_version == 1


def test_build_retro_dashboard_flags_retro_id_mismatch_between_analyses_and_context(tmp_path) -> None:
    """Fix 2: stale prior-run artifacts on disk must be flagged, not rendered as current."""
    retro_dir = tmp_path / "qa" / "results" / "retro"
    retro_dir.mkdir(parents=True)
    _write_analysis_files(retro_dir, retro_id="retro-stale")
    payload = _full_context_payload()
    payload["retro_id"] = "retro-fresh"
    (retro_dir / "context.json").write_text(json.dumps(payload), encoding="utf-8")

    doc = build_retro_dashboard(tmp_path)
    assert "retro_id_mismatch" in doc.run.integrity_reasons


def test_build_retro_dashboard_matching_retro_ids_do_not_flag_mismatch(tmp_path) -> None:
    """Fix 2 regression guard: matching retro_ids across analyses and context must not be flagged."""
    retro_dir = tmp_path / "qa" / "results" / "retro"
    retro_dir.mkdir(parents=True)
    _write_analysis_files(retro_dir, retro_id="retro-1")
    (retro_dir / "context.json").write_text(json.dumps(_full_context_payload()), encoding="utf-8")

    doc = build_retro_dashboard(tmp_path)
    assert "retro_id_mismatch" not in doc.run.integrity_reasons


def test_build_retro_dashboard_no_context_skips_retro_id_mismatch_check(tmp_path) -> None:
    """Fix 2: with no context to compare against, the mismatch check must not fire."""
    retro_dir = tmp_path / "qa" / "results" / "retro"
    retro_dir.mkdir(parents=True)
    _write_analysis_files(retro_dir, retro_id="retro-stale")

    doc = build_retro_dashboard(tmp_path)
    assert "retro_id_mismatch" not in doc.run.integrity_reasons


def test_build_retro_dashboard_flags_retro_id_mismatch_from_stale_status(tmp_path) -> None:
    """Fix A: a stale status.json (retro_id disagrees with context.json) must be flagged.

    If `reconcile` fails on a re-run but analyses+context are fresh, the
    previous run's status.json (with its improvement_ids) stays on disk.
    `_build_candidates` then positionally zips that stale `improvement_ids`
    tuple onto the current run's fresh `candidates`, so this must produce an
    integrity signal even though nothing else on disk disagrees.
    """
    root = _write_full_run(tmp_path)
    status_path = root / "qa" / "results" / "retro" / "status.json"
    status = json.loads(status_path.read_text(encoding="utf-8"))
    status["retro_id"] = "retro-stale-status"
    status_path.write_text(json.dumps(status), encoding="utf-8")

    doc = build_retro_dashboard(root)
    assert "retro_id_mismatch" in doc.run.integrity_reasons


def test_build_retro_dashboard_flags_retro_id_mismatch_from_stale_candidates(tmp_path) -> None:
    """Fix A: a stale candidates.json (retro_id disagrees with context.json) must be flagged.

    `candidates.json`'s real artifact shape (`ImprovementCandidateDocumentV3`)
    carries its own required top-level `retro_id`, so a leftover
    prior-run `candidates.json` must be caught the same way stale
    `status.json`/analysis files already are.
    """
    root = _write_full_run(tmp_path)
    candidates_path = root / "qa" / "results" / "retro" / "candidates.json"
    candidates = json.loads(candidates_path.read_text(encoding="utf-8"))
    candidates["retro_id"] = "retro-stale-candidates"
    candidates_path.write_text(json.dumps(candidates), encoding="utf-8")

    doc = build_retro_dashboard(root)
    assert "retro_id_mismatch" in doc.run.integrity_reasons


def test_build_retro_dashboard_matching_status_and_candidates_retro_ids_do_not_flag(tmp_path) -> None:
    """Fix A regression guard: a fully consistent full run must not be flagged."""
    root = _write_full_run(tmp_path)

    doc = build_retro_dashboard(root)
    assert "retro_id_mismatch" not in doc.run.integrity_reasons


def test_build_retro_dashboard_reports_unreadable_candidates_when_not_a_dict(tmp_path) -> None:
    """Fix 4: a top-level JSON array in candidates.json must fail loudly, not silently default."""
    retro_dir = tmp_path / "qa" / "results" / "retro"
    retro_dir.mkdir(parents=True)
    _write_analysis_files(retro_dir)
    (retro_dir / "candidates.json").write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")

    doc = build_retro_dashboard(tmp_path)
    assert "artifact_unreadable:candidates.json" in doc.run.integrity_reasons
    assert doc.candidates == ()


def test_build_retro_dashboard_reports_unreadable_candidates_when_key_missing(tmp_path) -> None:
    """Fix 4: a dict without a 'candidates' key must fail loudly, not silently default to ()."""
    retro_dir = tmp_path / "qa" / "results" / "retro"
    retro_dir.mkdir(parents=True)
    _write_analysis_files(retro_dir)
    (retro_dir / "candidates.json").write_text(json.dumps({"schema_version": "3"}), encoding="utf-8")

    doc = build_retro_dashboard(tmp_path)
    assert "artifact_unreadable:candidates.json" in doc.run.integrity_reasons
    assert doc.candidates == ()


def test_build_retro_dashboard_reports_unreadable_candidates_when_value_not_a_list(tmp_path) -> None:
    """Fix B: a non-list 'candidates' value must degrade gracefully, not raise an uncaught TypeError."""
    retro_dir = tmp_path / "qa" / "results" / "retro"
    retro_dir.mkdir(parents=True)
    _write_analysis_files(retro_dir)
    (retro_dir / "candidates.json").write_text(json.dumps({"candidates": None}), encoding="utf-8")

    doc = build_retro_dashboard(tmp_path)
    assert "artifact_unreadable:candidates.json" in doc.run.integrity_reasons
    assert doc.candidates == ()
