"""Tests for assurance_agent.workflow.improvements.review — pure review API.

Covers:
  - build_improvement_review_context: fields, allowed actions, delivery advice
  - validate_improvement_review_action: typed events, stale version, terminal
  - Context never expands Problem snapshots into writable fields
  - Advice payload fields do not alter canonical events
"""

from __future__ import annotations

import pytest

from assurance_agent.artifacts.models.improvements import (
    DeliveryKind,
    ImprovementKind,
    ImprovementProjection,
    ImprovementSourceRefs,
    ImprovementState,
    ImprovementVerification,
)
from assurance_agent.workflow.improvements.review import (
    REVIEW_ACTIONS,
    ReviewContextError,
    ReviewValidationError,
    build_improvement_review_context,
    validate_improvement_review_action,
)


def _projection(
    *,
    improvement_id: str = "IMP-ABCDEF0123456789FFFF",
    state: ImprovementState = ImprovementState.PROPOSED,
    version: int = 1,
    delivery: DeliveryKind = DeliveryKind.CHANGE_DRAFT,
    kind: ImprovementKind = ImprovementKind.WORKFLOW,
    knowledge_delta: dict | None = None,
) -> ImprovementProjection:
    overrides: dict = {}
    if knowledge_delta is not None:
        overrides["knowledge_delta"] = knowledge_delta
    return ImprovementProjection(
        improvement_id=improvement_id,
        fingerprint="a" * 64,
        kind=kind,
        delivery=delivery,
        source_refs=ImprovementSourceRefs(problem_ids=("PROB-1",)),
        target="assurance_agent/workflow/inspect",
        rationale="Repeated truncation",
        proposed_change="Preserve pytest E lines",
        verification=ImprovementVerification(
            suites=("workflow-full",),
            success_criteria="No truncation",
        ),
        risk="low",
        confidence="high",
        state=state,
        version=version,
        proposed_by_retro_ids=("RETRO-1",),
        last_event_id="IMPEVT-1",
        **overrides,
    )


# ---------------------------------------------------------------------------
# build_improvement_review_context
# ---------------------------------------------------------------------------


class TestBuildImprovementReviewContext:
    def test_captures_core_fields_without_problem_snapshots(self) -> None:
        proj = _projection()
        ctx = build_improvement_review_context(proj)
        assert ctx.improvement_id == proj.improvement_id
        assert ctx.expected_improvement_version == 1
        assert ctx.state is ImprovementState.PROPOSED
        assert ctx.source_refs.problem_ids == ("PROB-1",)
        assert ctx.target == proj.target
        assert ctx.proposed_change == proj.proposed_change
        assert ctx.verification.success_criteria == "No truncation"
        assert ctx.risk == "low"
        assert ctx.confidence == "high"
        # Source refs are IDs only — no Problem title/status/assessment expansion.
        dumped = ctx.model_dump(mode="json")
        assert "title" not in dumped
        assert "assessment" not in dumped
        assert "severity" not in dumped
        assert "status" not in dumped
        assert "occurrences" not in dumped

    def test_allowed_actions_for_proposed(self) -> None:
        ctx = build_improvement_review_context(_projection())
        assert set(ctx.allowed_actions) == {
            "approve",
            "reject",
            "request_rework",
            "supersede",
        }

    def test_allowed_actions_empty_for_terminal(self) -> None:
        for state in (ImprovementState.REJECTED, ImprovementState.SUPERSEDED):
            ctx = build_improvement_review_context(_projection(state=state))
            assert ctx.allowed_actions == ()

    def test_delivery_specific_advice_fields(self) -> None:
        change_ctx = build_improvement_review_context(
            _projection(delivery=DeliveryKind.CHANGE_DRAFT)
        )
        assert change_ctx.advice.delivery is DeliveryKind.CHANGE_DRAFT
        assert change_ctx.advice.change_draft_outline
        assert change_ctx.advice.memory_patch_path is None
        assert change_ctx.advice.knowledge_delta_summary is None

        mem_ctx = build_improvement_review_context(
            _projection(
                kind=ImprovementKind.PROMPT,
                delivery=DeliveryKind.MEMORY_PATCH,
            )
        )
        assert mem_ctx.advice.delivery is DeliveryKind.MEMORY_PATCH
        assert mem_ctx.advice.memory_patch_path
        assert mem_ctx.advice.change_draft_outline is None

        kd = {
            "schema_version": "1",
            "mode": "delta",
            "entities": {"dept": {"required_fields": ["name"]}},
        }
        know_ctx = build_improvement_review_context(
            _projection(
                kind=ImprovementKind.DOMAIN_KNOWLEDGE,
                delivery=DeliveryKind.KNOWLEDGE_DELTA,
                knowledge_delta=kd,
            )
        )
        assert know_ctx.advice.delivery is DeliveryKind.KNOWLEDGE_DELTA
        assert know_ctx.advice.knowledge_delta_summary
        assert know_ctx.advice.memory_patch_path is None

    def test_raises_when_projection_missing_required_identity(self) -> None:
        with pytest.raises(ReviewContextError):
            build_improvement_review_context(
                _projection(improvement_id="")  # type: ignore[arg-type]
            )


# ---------------------------------------------------------------------------
# validate_improvement_review_action
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("action", "event_type", "superseded_by"),
    [
        ("approve", "improvement_review_approved", None),
        ("reject", "improvement_review_rejected", None),
        ("request_rework", "improvement_rework_requested", None),
        ("supersede", "improvement_superseded", "IMP-NEW00000000000001"),
    ],
)
def test_review_action_builds_typed_event(
    action: str, event_type: str, superseded_by: str | None
) -> None:
    projection = _projection()
    event = validate_improvement_review_action(
        projection,
        action=action,
        reason="reviewed",
        who="alice",
        expected_version=projection.version,
        review_id="REV-1",
        superseded_by=superseded_by,
    )
    assert event.type == event_type
    assert event.improvement_id == projection.improvement_id
    assert event.expected_improvement_version == projection.version


class TestValidateImprovementReviewActionGuards:
    def test_stale_version_rejected(self) -> None:
        projection = _projection(version=2)
        with pytest.raises(ReviewValidationError, match="stale"):
            validate_improvement_review_action(
                projection,
                action="approve",
                reason="late",
                who="alice",
                expected_version=1,
                review_id="REV-1",
            )

    def test_terminal_state_rejected(self) -> None:
        for state in (ImprovementState.REJECTED, ImprovementState.SUPERSEDED):
            projection = _projection(state=state, version=3)
            with pytest.raises(ReviewValidationError, match="not allowed|terminal"):
                validate_improvement_review_action(
                    projection,
                    action="approve",
                    reason="noop",
                    who="alice",
                    expected_version=3,
                    review_id="REV-1",
                )

    def test_supersede_requires_superseded_by(self) -> None:
        with pytest.raises(ReviewValidationError, match="superseded_by"):
            validate_improvement_review_action(
                _projection(),
                action="supersede",
                reason="replaced",
                who="alice",
                expected_version=1,
                review_id="REV-1",
            )

    def test_advice_fields_in_payload_do_not_affect_event(self) -> None:
        """Delivery advice in resume payload must not change canonical outcome."""
        projection = _projection()
        event = validate_improvement_review_action(
            projection,
            action="approve",
            reason="reviewed",
            who="alice",
            expected_version=1,
            review_id="REV-1",
            payload={
                "advice": {
                    "delivery": "change_draft",
                    "checklist": ["ship it"],
                    "change_draft_outline": "rewrite authority",
                },
                "state": "applied",
                "severity": "critical",
            },
        )
        assert event.type == "improvement_review_approved"
        dumped = event.model_dump(mode="json")
        assert "advice" not in dumped
        assert "severity" not in dumped
        assert dumped["who"] == "alice"
        assert dumped["review_id"] == "REV-1"

    def test_unsupported_action_rejected(self) -> None:
        with pytest.raises(ReviewValidationError, match="unsupported"):
            validate_improvement_review_action(
                _projection(),
                action="start_work",
                reason="wrong lifecycle",
                who="alice",
                expected_version=1,
                review_id="REV-1",
            )

    def test_review_actions_constant(self) -> None:
        assert REVIEW_ACTIONS == frozenset(
            {"approve", "reject", "request_rework", "supersede"}
        )
