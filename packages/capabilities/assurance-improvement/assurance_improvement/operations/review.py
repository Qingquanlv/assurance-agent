"""Improvement review context, application, and auto-review handlers."""

from __future__ import annotations

from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from agent_runtime_contracts.ops import InputError, failed_input
from assurance_intake.contracts import EvidenceArtifactRefV1
from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest

from assurance_improvement.contracts.improvements import (
    ImprovementProjection,
    ImprovementState,
    LastAutoReview,
)
from assurance_improvement.contracts.review import (
    AppliedAutoReviewV1,
    ApplyReviewPublishedV1,
    ImprovementAutoReviewAssessment,
    ImprovementAutoReviewStatus,
    auto_review_route,
    human_review_route,
    projection_state_name,
)
from assurance_improvement.contracts.delivery import artifact_digest
from assurance_improvement.contracts.decisions import AUTO_REVIEW_DECISIONS
from assurance_improvement.operations.common import succeeded, validate_input
from assurance_improvement.operations.keys import improvement_event_id

_FROZEN = ConfigDict(frozen=True, extra="forbid")

REVIEW_ACTIONS: frozenset[str] = frozenset({"approve", "reject", "request_rework", "supersede"})
_ACTION_TARGET: dict[str, ImprovementState] = {
    "approve": ImprovementState.APPROVED,
    "reject": ImprovementState.REJECTED,
    "request_rework": ImprovementState.NEEDS_REWORK,
    "supersede": ImprovementState.SUPERSEDED,
}
_AUTO_REVIEW: dict[
    str,
    tuple[
        Literal["approved", "escalated"],
        ImprovementState | None,
        Literal["auto_approved", "changes_requested", "needs_human_review", "reject_advice"],
    ],
] = {
    "pass": ("approved", ImprovementState.APPROVED, "auto_approved"),
    "changes_requested": ("escalated", ImprovementState.NEEDS_REWORK, "changes_requested"),
    "needs_human_review": ("escalated", None, "needs_human_review"),
    "reject": ("escalated", ImprovementState.REJECTED, "reject_advice"),
}
_ALLOWED: dict[ImprovementState, frozenset[ImprovementState]] = {
    ImprovementState.PROPOSED: frozenset(
        {
            ImprovementState.APPROVED,
            ImprovementState.REJECTED,
            ImprovementState.NEEDS_REWORK,
            ImprovementState.SUPERSEDED,
        }
    ),
    ImprovementState.APPROVED: frozenset(
        {
            ImprovementState.EVALUATING,
            ImprovementState.EXPORTED,
            ImprovementState.REJECTED,
            ImprovementState.NEEDS_REWORK,
            ImprovementState.SUPERSEDED,
        }
    ),
    ImprovementState.NEEDS_REWORK: frozenset({ImprovementState.PROPOSED, ImprovementState.SUPERSEDED}),
    ImprovementState.EVALUATING: frozenset(
        {
            ImprovementState.APPLIED,
            ImprovementState.ROLLED_BACK,
            ImprovementState.AWAITING_BASELINE,
            ImprovementState.EVAL_ERROR,
            ImprovementState.SUPERSEDED,
        }
    ),
    ImprovementState.AWAITING_BASELINE: frozenset({ImprovementState.EVALUATING, ImprovementState.SUPERSEDED}),
    ImprovementState.EVAL_ERROR: frozenset({ImprovementState.EVALUATING, ImprovementState.SUPERSEDED}),
    ImprovementState.EXPORTED: frozenset(
        {ImprovementState.APPLIED, ImprovementState.NEEDS_REWORK, ImprovementState.SUPERSEDED}
    ),
    ImprovementState.APPLIED: frozenset({ImprovementState.ROLLED_BACK, ImprovementState.SUPERSEDED}),
    ImprovementState.ROLLED_BACK: frozenset({ImprovementState.NEEDS_REWORK, ImprovementState.SUPERSEDED}),
    ImprovementState.REJECTED: frozenset(),
    ImprovementState.SUPERSEDED: frozenset(),
}


def assert_improvement_transition(current: ImprovementState, target: ImprovementState) -> None:
    if current is target:
        raise InputError(f"self-transition is not allowed for state {current.value}")
    if target not in _ALLOWED.get(current, frozenset()):
        raise InputError(f"transition {current.value} -> {target.value} is not allowed")


class ApplyAutoReviewInput(BaseModel):
    model_config = _FROZEN

    assessment: ImprovementAutoReviewAssessment
    current: ImprovementProjection


class ApplyReviewInput(BaseModel):
    model_config = _FROZEN

    projection: ImprovementProjection | None = None
    projection_ref: EvidenceArtifactRefV1 | None = None
    action: str
    review_id: str = Field(min_length=1)
    expected_improvement_version: int | None = Field(default=None, ge=1)


def apply_review(payload: ApplyReviewInput) -> ImprovementProjection:
    if payload.projection is None or payload.expected_improvement_version is None:
        raise InputError("review requires the current projection")
    if payload.action not in REVIEW_ACTIONS:
        raise InputError(f"unsupported review action: {payload.action}")
    if payload.expected_improvement_version != payload.projection.version:
        raise InputError("review version does not match the current improvement")
    target = _ACTION_TARGET[payload.action]
    assert_improvement_transition(payload.projection.state, target)
    event_id = improvement_event_id(payload.review_id, payload.action, payload.projection.version)
    return payload.projection.model_copy(
        update={
            "state": target,
            "version": payload.projection.version + 1,
            "last_event_id": event_id,
            "approval_source": "human" if payload.action == "approve" else "none",
        }
    )


def apply_auto_review(
    payload: ApplyAutoReviewInput,
) -> tuple[ImprovementAutoReviewStatus, ImprovementProjection]:
    if payload.assessment.improvement_id != payload.current.improvement_id:
        raise InputError("auto-review improvement_id does not match")
    if payload.assessment.expected_improvement_version != payload.current.version:
        raise InputError("auto-review version does not match")
    if payload.assessment.decision not in AUTO_REVIEW_DECISIONS:
        raise InputError(f"unsupported auto-review decision: {payload.assessment.decision}")
    result, target, verdict = _AUTO_REVIEW[payload.assessment.decision]
    next_state = target if target is not None else payload.current.state
    if target is not None:
        assert_improvement_transition(payload.current.state, next_state)
    updated = payload.current.model_copy(
        update={
            "state": next_state,
            "version": payload.current.version + (1 if target is not None else 0),
            "approval_source": "automatic" if result == "approved" else "none",
            "last_auto_review": LastAutoReview(
                review_id=payload.assessment.review_id,
                subject_sha256=payload.assessment.subject_sha256,
                assessment_sha256=artifact_digest(payload.assessment),
                policy_version="1",
                verdict=verdict,
            ),
        }
    )
    status = ImprovementAutoReviewStatus(
        review_id=payload.assessment.review_id,
        improvement_id=payload.assessment.improvement_id,
        result=result,
    )
    return status, updated


class ApplyImprovementAutoReviewHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        from assurance_improvement.contracts.handoff import PROJECTION
        from assurance_improvement.operations.files import stage_named

        try:
            payload = validate_input(ApplyAutoReviewInput, request.input)
            status, updated = apply_auto_review(payload)
            stage_named(context, PROJECTION, updated)
            result = AppliedAutoReviewV1(
                status=status,
                projection=updated,
                route=auto_review_route(projection_state_name(updated)),
            )
            return succeeded(cast(dict[str, object], result.model_dump(mode="json")))
        except InputError as error:
            return failed_input(error)


class ApplyImprovementReviewHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        from assurance_improvement.contracts.handoff import PROJECTION
        from assurance_improvement.operations.files import load_named, stage_named

        try:
            payload = validate_input(ApplyReviewInput, request.input)
            projection = (
                load_named(context, payload.projection_ref, ImprovementProjection)
                if payload.projection_ref is not None
                else payload.projection
            )
            version = (
                payload.expected_improvement_version
                if payload.expected_improvement_version is not None
                else None
                if projection is None
                else projection.version
            )
            updated = apply_review(
                payload.model_copy(update={"projection": projection, "expected_improvement_version": version})
            )
            stage_named(context, PROJECTION, updated)
            published = ApplyReviewPublishedV1(
                projection=updated,
                route=human_review_route(projection_state_name(updated)),
            )
            return succeeded(cast(dict[str, object], published.model_dump(mode="json")))
        except InputError as error:
            return failed_input(error)


__all__ = [
    "ApplyImprovementAutoReviewHandler",
    "ApplyImprovementReviewHandler",
    "apply_auto_review",
    "apply_review",
    "assert_improvement_transition",
]
