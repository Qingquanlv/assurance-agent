"""Retro v3 artifact 模型契约测试（spec C1 验收）。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from assurance_agent.artifacts.models.retro_v3 import (
    EvalEvidenceSlice,
    EvalTrendSignal,
    GateVerdictEvidenceEntry,
    GatePushbackSignal,
    HealingOutcomeEvidenceEntry,
    HealingSignal,
    ImprovementCandidateDocumentDraftV3,
    ImprovementCandidateDocumentV3,
    ImprovementCandidateV3,
    IssueEvidenceSlice,
    IssuePatternSignal,
    RetroContextV3,
    SkillDriftEvidenceEntry,
    SkillDriftSignal,
    SignalDocumentV3,
    SignalDraftDocument,
    TaskFailureEvidenceEntry,
    TaskFailureSignal,
    WorkflowEvidenceSlice,
)

WINDOW = {
    "selection": {"mode": "last", "requested_last": 10},
    "change_ids": ["CH-1", "CH-2"],
    "since": None,
    "until": None,
    "project_event_through": None,
}

REFS = {"problem_ids": ["PROB-1"], "occurrence_ids": ["OCC-1"]}

BASE_SIGNAL = {
    "signal_id": "issue-pattern:l1_knowledge_missing_symbol",
    "summary": "Existing user/role adapters are absent from L1 capability registry",
    "occurrence_count": 3,
    "recommended_change": "Register the existing adapter and factory symbols",
    "source_refs": REFS,
    "confidence": "high",
}

ISSUE_SIGNAL = {
    **BASE_SIGNAL,
    "signal_type": "issue_pattern",
    "pattern_kind": "knowledge_gap",
    "affected_surface": {"kind": "knowledge_registry", "value": ".aa/data-knowledge.yaml"},
    "symptom": "Existing user/role adapters are absent from L1 capability registry",
}

DRAFT_DOC = {
    "schema_version": "3",
    "retro_id": "RET-1",
    "domain": "issue",
    "analysis_status": "ok",
    "failure_reason": None,
    "analyzer": "aa-retro-issue-analysis",
    "signals": [ISSUE_SIGNAL],
}


def test_six_signal_types_validate() -> None:
    assert IssuePatternSignal.model_validate(ISSUE_SIGNAL).signal_type == "issue_pattern"
    assert (
        GatePushbackSignal.model_validate(
            {
                **BASE_SIGNAL,
                "signal_type": "gate_pushback",
                "gate_id": "archive-gate",
                "cause": "archive.execution_failed",
            }
        ).gate_id
        == "archive-gate"
    )
    assert (
        TaskFailureSignal.model_validate(
            {
                **BASE_SIGNAL,
                "signal_type": "task_failure",
                "node_id": "execution",
                "error_kind": "timeout",
                "message_fingerprint": "fp:123",
            }
        ).error_kind
        == "timeout"
    )
    assert (
        HealingSignal.model_validate(
            {
                **BASE_SIGNAL,
                "signal_type": "healing",
                "operation": "apply-fix",
                "outcome": "regressed",
            }
        ).outcome
        == "regressed"
    )
    assert (
        SkillDriftSignal.model_validate(
            {
                **BASE_SIGNAL,
                "signal_type": "skill_drift",
                "phase": "codegen",
                "expected_skill": "aa-api-codegen",
            }
        ).expected_skill
        == "aa-api-codegen"
    )
    assert (
        EvalTrendSignal.model_validate(
            {
                **BASE_SIGNAL,
                "signal_type": "eval_trend",
                "suite": "workflow-full",
                "verdict": "inconclusive",
                "failure_signature": "baseline_missing",
                "consecutive_count": 2,
                "sample_run_ids": ["eval-1"],
                "source_change_ids": ["RET-user-1"],
            }
        ).consecutive_count
        == 2
    )


def test_signal_rejects_forbidden_fields() -> None:
    for field in (
        "status",
        "version",
        "disposition",
        "human_confirmed",
        "severity",
        "classification",
        "patch",
        "instructions",
    ):
        with pytest.raises(ValidationError):
            IssuePatternSignal.model_validate({**ISSUE_SIGNAL, field: "x"})


def test_signal_rejects_empty_source_refs() -> None:
    with pytest.raises(ValidationError):
        IssuePatternSignal.model_validate({**ISSUE_SIGNAL, "source_refs": {}})


def test_signal_rejects_unknown_confidence() -> None:
    with pytest.raises(ValidationError):
        IssuePatternSignal.model_validate({**ISSUE_SIGNAL, "confidence": "certain"})


def test_signal_rejects_missing_type_required_field() -> None:
    bad = {**ISSUE_SIGNAL, "signal_type": "eval_trend"}
    with pytest.raises(ValidationError):
        EvalTrendSignal.model_validate(bad)  # 缺 suite/verdict/failure_signature


def test_draft_document_rejects_slice_sha256() -> None:
    with pytest.raises(ValidationError):
        SignalDraftDocument.model_validate({**DRAFT_DOC, "slice_sha256": "abc"})


def test_canonical_document_requires_slice_sha256() -> None:
    with pytest.raises(ValidationError):
        SignalDocumentV3.model_validate(DRAFT_DOC)
    doc = SignalDocumentV3.model_validate({**DRAFT_DOC, "slice_sha256": "abc"})
    assert doc.slice_sha256 == "abc"


def test_failed_document_requires_reason_and_no_signals() -> None:
    with pytest.raises(ValidationError):
        SignalDraftDocument.model_validate(
            {**DRAFT_DOC, "analysis_status": "failed", "failure_reason": None, "signals": []}
        )
    with pytest.raises(ValidationError):
        SignalDraftDocument.model_validate(
            {**DRAFT_DOC, "analysis_status": "failed", "failure_reason": "timeout"}
        )  # failed 不得带 signals
    ok = SignalDraftDocument.model_validate(
        {**DRAFT_DOC, "analysis_status": "failed", "failure_reason": "timeout", "signals": []}
    )
    assert ok.analysis_status == "failed"


def test_ok_document_rejects_failure_reason() -> None:
    with pytest.raises(ValidationError):
        SignalDraftDocument.model_validate({**DRAFT_DOC, "failure_reason": "boom"})


def test_issue_slice_typed_entries() -> None:
    slice_ = IssueEvidenceSlice.model_validate(
        {
            "schema_version": "3",
            "retro_id": "RET-1",
            "domain": "issue",
            "window": WINDOW,
            "sources": [
                {
                    "kind": "project_problem_ledger",
                    "change_id": None,
                    "head_event_id": "evt-1",
                    "sha256": "abc",
                    "evidence_ids": ["PROB-1", "OCC-1"],
                }
            ],
            "entries": [
                {
                    "occurrence_id": "OCC-1",
                    "problem_id": "PROB-1",
                    "change_id": "CH-1",
                    "batch_id": "B-1",
                    "surface": {"kind": "endpoint", "value": "GET /api/v1/users"},
                    "symptom": "http 500",
                    "fingerprint": "fp:1",
                    "observed_at": "2026-07-01T00:00:00Z",
                }
            ],
        }
    )
    assert slice_.entries[0].occurrence_id == "OCC-1"
    assert slice_.resolvable_ids() == frozenset({"PROB-1", "OCC-1"})


def test_workflow_and_eval_slice_typed_entries() -> None:
    wf = WorkflowEvidenceSlice.model_validate(
        {
            "schema_version": "3",
            "retro_id": "RET-1",
            "domain": "workflow",
            "window": WINDOW,
            "sources": [
                {
                    "kind": "workflow_ledger",
                    "change_id": "CH-1",
                    "head_event_id": "CH-1#seq9",
                    "sha256": "abc",
                    "evidence_ids": [
                        "CH-1#seq2",
                        "CH-1#seq3",
                        "CH-1#healing:api.json",
                        "CH-1#workflow-state:explore",
                    ],
                }
            ],
            "entries": [
                {
                    "entry_kind": "gate_verdict",
                    "evidence_id": "CH-1#seq2",
                    "change_id": "CH-1",
                    "gate_id": "archive-gate",
                    "verdict": "stop",
                    "cause": "archive.execution_failed",
                    "reason": "execution failed",
                    "ts": "2026-07-01T00:00:00Z",
                },
                {
                    "entry_kind": "task_failure",
                    "evidence_id": "CH-1#seq3",
                    "change_id": "CH-1",
                    "task_id": "task-1",
                    "attempt_id": "task-1-a1",
                    "node_id": "execution",
                    "error_kind": "timeout",
                    "message_fingerprint": "sha256:123",
                    "recovered": False,
                    "ts": "2026-07-01T00:00:01Z",
                },
                {
                    "entry_kind": "healing_outcome",
                    "evidence_id": "CH-1#healing:api.json",
                    "change_id": "CH-1",
                    "operation": "api",
                    "outcome": "applied",
                },
                {
                    "entry_kind": "skill_drift",
                    "evidence_id": "CH-1#workflow-state:explore",
                    "change_id": "CH-1",
                    "phase": "explore",
                    "expected_skill": "aa-explore",
                },
            ],
        }
    )
    assert isinstance(wf.entries[0], GateVerdictEvidenceEntry)
    assert wf.entries[0].cause == "archive.execution_failed"
    assert isinstance(wf.entries[1], TaskFailureEvidenceEntry)
    assert wf.entries[1].task_id == "task-1"
    assert isinstance(wf.entries[2], HealingOutcomeEvidenceEntry)
    assert wf.entries[2].operation == "api"
    assert isinstance(wf.entries[3], SkillDriftEvidenceEntry)
    assert wf.entries[3].phase == "explore"
    assert wf.resolvable_ids() == frozenset(
        {
            "CH-1#seq2",
            "CH-1#seq3",
            "CH-1#healing:api.json",
            "CH-1#workflow-state:explore",
        }
    )
    ev = EvalEvidenceSlice.model_validate(
        {
            "schema_version": "3",
            "retro_id": "RET-1",
            "domain": "eval",
            "window": WINDOW,
            "sources": [],
            "entries": [
                {
                    "run_id": "eval-1",
                    "suite": "workflow-full",
                    "verdict": "inconclusive",
                    "started_at": "2026-07-01T00:00:00Z",
                }
            ],
        }
    )
    assert ev.entries[0].source_change_ids is None  # legacy 缺失 = 无关联信息


def test_workflow_entry_requires_immutable_evidence_id() -> None:
    with pytest.raises(ValidationError):
        WorkflowEvidenceSlice.model_validate(
            {
                "schema_version": "3",
                "retro_id": "RET-1",
                "domain": "workflow",
                "window": WINDOW,
                "sources": [],
                "entries": [
                    {
                        "entry_kind": "skill_drift",
                        "change_id": "CH-1",
                        "phase": "inspect",
                    }
                ],
            }
        )


def test_context_v3_and_domain_status() -> None:
    ctx = RetroContextV3.model_validate(
        {
            "schema_version": "3",
            "retro_id": "RET-1",
            "generated_at": "2026-07-26T00:00:00Z",
            "dry_run": False,
            "window": WINDOW,
            "source_manifest": {
                "issue_slice_sha256": "a",
                "workflow_slice_sha256": "b",
                "eval_slice_sha256": "c",
                "issue_sources": [],
                "workflow_sources": [],
                "eval_sources": [],
            },
            "integrity": {"status": "complete", "reasons": []},
            "domain_status": {
                "issue": {"status": "ok", "failure_reason": None},
                "workflow": {"status": "failed", "failure_reason": "timeout"},
                "eval": {"status": "ok", "failure_reason": None},
            },
            "signals": {"issue": [ISSUE_SIGNAL], "workflow": [], "eval": []},
            "signal_count": 1,
        }
    )
    assert ctx.signal_count == 1
    assert ctx.domain_status.workflow.failure_reason == "timeout"
    assert ctx.allows_domain_knowledge is True


def test_candidate_v3_requires_signal_ids() -> None:
    candidate = {
        "candidate_id": "C-1",
        "kind": "workflow_improvement",
        "delivery": "change_draft",
        "source_refs": REFS,
        "target": "assurance_agent/_resources/schemas/",
        "rationale": "L1 registry gaps repeatedly block codegen",
        "proposed_change": "Register existing adapter symbols",
        "verification": {"suites": [], "required_cases": [], "success_criteria": "manual review"},
        "risk": "low",
        "confidence": "high",
    }
    with pytest.raises(ValidationError):
        ImprovementCandidateV3.model_validate(candidate)  # 缺 signal_ids
    ok = ImprovementCandidateV3.model_validate({**candidate, "signal_ids": ["issue-pattern:x"]})
    assert ok.signal_ids == ("issue-pattern:x",)


def test_candidate_document_draft_vs_canonical() -> None:
    doc = {
        "schema_version": "3",
        "retro_id": "RET-1",
        "candidates": [],
    }
    with pytest.raises(ValidationError):
        ImprovementCandidateDocumentDraftV3.model_validate({**doc, "context_sha256": "x"})
    with pytest.raises(ValidationError):
        ImprovementCandidateDocumentV3.model_validate(doc)
    assert ImprovementCandidateDocumentV3.model_validate({**doc, "context_sha256": "x"}).retro_id == "RET-1"
