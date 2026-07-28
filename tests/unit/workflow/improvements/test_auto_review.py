"""Policy and selection tests for bounded automatic Improvement review."""

from __future__ import annotations

from typing import Literal

import pytest

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
    select_auto_review_items,
)
from assurance_agent.workflow.improvements.reconciler import ImprovementAcceptStatus


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
