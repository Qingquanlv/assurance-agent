"""Policy and selection tests for bounded automatic Improvement review."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import pytest

from assurance_agent.artifacts.canonical import canonical_json_bytes, sha256_bytes
from assurance_agent.artifacts.models.improvement_review import ImprovementAutoReviewAssessment
from assurance_agent.artifacts.models.improvements import (
    DeliveryKind,
    ImprovementKind,
    ImprovementLedgerProjection,
    ImprovementProjection,
    ImprovementSourceRefs,
    ImprovementState,
    ImprovementVerification,
    LastAutoReview,
)
from assurance_agent.workflow.improvements.auto_review import (
    AutoReviewGateInput,
    _delivery_allowed,
    build_auto_review_gate_input,
    select_auto_review_items,
)
from assurance_agent.workflow.improvements.events import ImprovementProposedEvent
from assurance_agent.workflow.improvements.reconciler import ImprovementAcceptStatus
from assurance_agent.workflow.improvements.review_subject import build_review_subject
from tests.unit.workflow.improvements.test_reconcile_v3 import _candidate, _context


SUBJECT = "sha256:" + "a" * 64
ASSESSMENT = "sha256:" + "b" * 64


def _gate() -> AutoReviewGateInput:
    return AutoReviewGateInput(
        review_id="AUTO-1",
        improvement_id="IMP-1",
        expected_improvement_version=1,
        subject_sha256=SUBJECT,
        assessment_sha256=ASSESSMENT,
        projection_state="proposed",
        projection_subject_matches=True,
        semantic_subject_matches=True,
        kind_allowed=True,
        delivery_allowed=True,
        risk_low=True,
        confidence_high=True,
        reviewer_pass=True,
        human_review_required=False,
        source_refs_resolve=True,
        has_blocking_findings=False,
        verification_ready=True,
        ambiguity_free=True,
        prior_terminal_review=False,
        explicit_retry_allowed=False,
    )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("projection_state", "approved"),
        ("projection_subject_matches", False),
        ("semantic_subject_matches", False),
        ("kind_allowed", False),
        ("delivery_allowed", False),
        ("risk_low", False),
        ("confidence_high", False),
        ("reviewer_pass", False),
        ("human_review_required", True),
        ("source_refs_resolve", False),
        ("has_blocking_findings", True),
        ("verification_ready", False),
        ("ambiguity_free", False),
        ("prior_terminal_review", True),
    ),
)
def test_each_mechanical_policy_condition_fails_closed(field: str, value: object) -> None:
    assert not _gate().model_copy(update={field: value}).auto_approve


def test_all_mechanical_policy_conditions_allow_approval() -> None:
    assert _gate().auto_approve


def test_auto_review_rejects_legacy_invalid_memory_target() -> None:
    assert not _delivery_allowed(
        DeliveryKind.MEMORY_PATCH,
        "skills/awe-api-plan:required-field-summary-probe",
    )


def test_auto_review_rejects_nul_memory_target() -> None:
    assert not _delivery_allowed(DeliveryKind.MEMORY_PATCH, ".aa/memory/bad\x00name.md")


def test_auto_review_allows_valid_memory_target_and_existing_change_draft() -> None:
    assert _delivery_allowed(DeliveryKind.MEMORY_PATCH, ".aa/memory/aa-api-plan.md")
    assert _delivery_allowed(DeliveryKind.CHANGE_DRAFT, "qa/planning:api-rule")
    assert not _delivery_allowed(DeliveryKind.KNOWLEDGE_DELTA, "qa/knowledge:entities.dept")


def test_auto_review_gate_projects_real_history_before_rejecting_legacy_target(
    tmp_path: Path,
) -> None:
    subject, _digest, _data = build_review_subject(_candidate(), _context(), improvement_id="IMP-1")
    legacy_subject = subject.model_copy(
        update={
            "kind": ImprovementKind.PROMPT,
            "delivery": DeliveryKind.MEMORY_PATCH,
            "target": "skills/awe-api-plan:required-field-summary-probe",
        }
    )
    subject_bytes = canonical_json_bytes(legacy_subject)
    subject_sha256 = sha256_bytes(subject_bytes)
    root = tmp_path / "qa" / "improvements"
    subject_path = root / "review-subjects" / f"{subject_sha256}.json"
    subject_path.parent.mkdir(parents=True)
    subject_path.write_bytes(subject_bytes)
    assessment = ImprovementAutoReviewAssessment(
        review_id="AUTO-1",
        improvement_id="IMP-1",
        expected_improvement_version=1,
        subject_sha256=subject_sha256,
        decision="pass",
        evidence_traceability="complete",
        scope_readiness="ready",
        verification_readiness="ready",
        delivery_safety="ready",
        human_review_required=False,
    )
    assessment_path = root / "reviews" / "AUTO-1" / "assessment.json"
    assessment_path.parent.mkdir(parents=True)
    assessment_path.write_bytes(canonical_json_bytes(assessment))
    legacy_event = ImprovementProposedEvent(
        schema_version="1.0",
        seq=2,
        event_id="IMPEVT-LEGACY-MEMORY-TARGET",
        idempotency_key="legacy:memory-target",
        ts="2026-08-03T00:00:00Z",
        improvement_id="IMP-1",
        expected_improvement_version=0,
        type="improvement_proposed",
        fingerprint="legacy-memory-target",
        fingerprint_version="1",
        kind=legacy_subject.kind,
        delivery=legacy_subject.delivery,
        source_refs=legacy_subject.source_refs,
        target=legacy_subject.target,
        rationale=legacy_subject.rationale,
        proposed_change=legacy_subject.proposed_change,
        verification=legacy_subject.verification,
        risk=legacy_subject.risk,
        confidence=legacy_subject.confidence,
        retro_id="RETRO-LEGACY",
        candidate_id="IMP-CAND-LEGACY",
        context_sha256="sha256:legacy-context",
        candidate_batch_digest="sha256:legacy-batch",
        review_subject_sha256=subject_sha256,
    )
    historical_event_fixture = (
        Path(__file__).resolve().parents[3]
        / "fixtures"
        / "improvements"
        / "historical-max-length"
        / "events.jsonl"
    )
    events_path = root / "events.jsonl"
    events_path.parent.mkdir(parents=True, exist_ok=True)
    events_path.write_bytes(historical_event_fixture.read_bytes() + canonical_json_bytes(legacy_event))

    gate = build_auto_review_gate_input(
        tmp_path,
        review_id="AUTO-1",
        improvement_id="IMP-1",
        subject_sha256=subject_sha256,
        expected_version=1,
        policy_version="1",
        attempt=1,
    )

    assert gate.projection_state == "proposed"
    assert gate.projection_subject_matches
    assert not gate.delivery_allowed
    assert not gate.auto_approve


def _projection(*, last_review: LastAutoReview | None = None) -> ImprovementLedgerProjection:
    item = ImprovementProjection(
        improvement_id="IMP-1",
        fingerprint="f" * 64,
        kind=ImprovementKind.WORKFLOW,
        delivery=DeliveryKind.CHANGE_DRAFT,
        source_refs=ImprovementSourceRefs(problem_ids=("PROB-1",)),
        target="workflow.retro",
        rationale="Close a repeated process gap",
        proposed_change="Add bounded automatic review",
        verification=ImprovementVerification(
            suites=("workflow",), success_criteria="Eligible proposal is approved"
        ),
        risk="low",
        confidence="high",
        state=ImprovementState.PROPOSED,
        version=1,
        proposed_by_retro_ids=("RETRO-1",),
        last_event_id="EVT-1",
        review_subject_sha256=SUBJECT,
        last_auto_review=last_review,
    )
    return ImprovementLedgerProjection(
        schema_version="1",
        last_seq=1,
        improvements={item.improvement_id: item},
        by_fingerprint={item.fingerprint: item.improvement_id},
    )


def _receipt(*, result: Literal["accepted", "failed"] = "accepted") -> ImprovementAcceptStatus:
    return ImprovementAcceptStatus(
        retro_id="RETRO-1",
        context_sha256="sha256:" + "c" * 64,
        candidate_batch_digest="sha256:" + "d" * 64,
        idempotency_key="retro-1",
        result=result,
        improvement_ids=("IMP-1",),
        event_ids=("EVT-1",),
    )


def test_selector_only_uses_explicit_accepted_receipts() -> None:
    assert len(select_auto_review_items(_projection(), (_receipt(),))) == 1
    assert select_auto_review_items(_projection(), (_receipt(result="failed"),)) == ()
    assert select_auto_review_items(_projection(), ()) == ()


def test_selector_does_not_recurse_after_same_subject_review_error() -> None:
    prior = LastAutoReview(
        review_id="AUTO-OLD",
        subject_sha256=SUBJECT,
        assessment_sha256=ASSESSMENT,
        policy_version="1",
        verdict="review_error",
    )
    assert select_auto_review_items(_projection(last_review=prior), (_receipt(),)) == ()
