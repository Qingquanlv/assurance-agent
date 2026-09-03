"""Improvement review context, application, and auto-review handlers."""

from __future__ import annotations

from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest

from assurance_improvement.contracts.improvements import (
    ImprovementLedgerProjection,
    ImprovementProjection,
    ImprovementReviewAdvice,
    ImprovementReviewContext,
    ImprovementState,
    LastAutoReview,
)
from assurance_improvement.contracts.review import (
    AutoReviewBatchError,
    ImprovementAutoReviewAssessment,
    ImprovementAutoReviewAssessmentAuthoring,
    ImprovementAutoReviewBatchSummary,
    ImprovementAutoReviewStatus,
    ImprovementReviewSubject,
)
from assurance_improvement.contracts.delivery import artifact_digest
from assurance_improvement.contracts.decisions import AUTO_REVIEW_DECISIONS
from assurance_improvement.operations.common import InputError, failed_input, succeeded, validate_input
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


class LoadReviewSubjectInput(BaseModel):
    model_config = _FROZEN

    subject: ImprovementReviewSubject


class ValidateAssessmentInput(BaseModel):
    model_config = _FROZEN

    assessment: ImprovementAutoReviewAssessmentAuthoring
    subject: ImprovementReviewSubject
    current: ImprovementProjection
    review_id: str = Field(min_length=1)


class ApplyAutoReviewInput(BaseModel):
    model_config = _FROZEN

    assessment: ImprovementAutoReviewAssessment
    current: ImprovementProjection


class AutoReviewErrorInput(BaseModel):
    model_config = _FROZEN

    review_id: str = Field(min_length=1)
    improvement_id: str = Field(min_length=1)
    error_kind: str = Field(min_length=1)


class OrchestrationErrorInput(BaseModel):
    model_config = _FROZEN

    retro_id: str = Field(min_length=1)
    stage: Literal["selector", "fan_out", "summarize"]
    error_kind: str = Field(min_length=1)


class SelectAutoReviewInput(BaseModel):
    model_config = _FROZEN

    retro_id: str = Field(min_length=1)
    ledger: ImprovementLedgerProjection


class SummarizeBatchInput(BaseModel):
    model_config = _FROZEN

    retro_id: str = Field(min_length=1)
    statuses: tuple[ImprovementAutoReviewStatus, ...] = ()
    orchestration_errors: tuple[AutoReviewBatchError, ...] = ()


class LoadReviewContextInput(BaseModel):
    model_config = _FROZEN

    projection: ImprovementProjection
    allowed_actions: tuple[str, ...] = ()


class ApplyReviewInput(BaseModel):
    model_config = _FROZEN

    projection: ImprovementProjection
    action: str
    review_id: str = Field(min_length=1)
    expected_improvement_version: int = Field(ge=1)


def apply_review(payload: ApplyReviewInput) -> ImprovementProjection:
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


class LoadReviewSubjectHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            payload = validate_input(LoadReviewSubjectInput, request.input)
            return succeeded(cast(dict[str, object], payload.subject.model_dump(mode="json")))
        except InputError as error:
            return failed_input(error)


class ValidateImprovementReviewAssessmentHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            payload = validate_input(ValidateAssessmentInput, request.input)
            if payload.subject.improvement_id != payload.current.improvement_id:
                raise InputError("assessment subject does not match the current improvement")
            bound = ImprovementAutoReviewAssessment.model_validate(
                {
                    **payload.assessment.model_dump(mode="json"),
                    "review_id": payload.review_id,
                    "improvement_id": payload.subject.improvement_id,
                    "expected_improvement_version": payload.current.version,
                    "subject_sha256": payload.subject.provenance.context_sha256,
                }
            )
            return succeeded(cast(dict[str, object], bound.model_dump(mode="json")))
        except InputError as error:
            return failed_input(error)


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
        del context
        try:
            payload = validate_input(ApplyAutoReviewInput, request.input)
            status, updated = apply_auto_review(payload)
            dumped = updated.model_dump(mode="json")
            return succeeded(
                {
                    "status": status.model_dump(mode="json"),
                    "projection": dumped,
                    "lifecycle_state": updated.state.value,
                    "effect_intents": [],
                    "write_authorization": [],
                }
            )
        except InputError as error:
            return failed_input(error)


class RecordImprovementAutoReviewErrorHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            payload = validate_input(AutoReviewErrorInput, request.input)
            status = ImprovementAutoReviewStatus(
                review_id=payload.review_id,
                improvement_id=payload.improvement_id,
                result="review_error",
            )
            return succeeded(cast(dict[str, object], status.model_dump(mode="json")))
        except InputError as error:
            return failed_input(error)


class RecordAutoReviewOrchestrationErrorHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            payload = validate_input(OrchestrationErrorInput, request.input)
            return succeeded(
                {
                    "retro_id": payload.retro_id,
                    "error": AutoReviewBatchError(
                        stage=payload.stage, error_kind=payload.error_kind
                    ).model_dump(mode="json"),
                }
            )
        except InputError as error:
            return failed_input(error)


class SelectCurrentRetroAutoReviewItemsHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            payload = validate_input(SelectAutoReviewInput, request.input)
            selected = tuple(
                item.improvement_id
                for item in payload.ledger.improvements.values()
                if payload.retro_id in item.proposed_by_retro_ids and item.state is ImprovementState.PROPOSED
            )
            return succeeded({"improvement_ids": list(selected)})
        except InputError as error:
            return failed_input(error)


class SummarizeAutoReviewBatchHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            payload = validate_input(SummarizeBatchInput, request.input)
            counts = {"approved": 0, "escalated": 0, "review_error": 0, "stale": 0}
            for status in payload.statuses:
                counts[status.result] += 1
            summary = ImprovementAutoReviewBatchSummary(
                retro_id=payload.retro_id,
                review_ids=tuple(item.review_id for item in payload.statuses),
                approved=counts["approved"],
                escalated=counts["escalated"],
                errors=counts["review_error"],
                stale=counts["stale"],
                orchestration_errors=payload.orchestration_errors,
            )
            return succeeded(cast(dict[str, object], summary.model_dump(mode="json")))
        except InputError as error:
            return failed_input(error)


class LoadImprovementReviewContextHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            payload = validate_input(LoadReviewContextInput, request.input)
            allowed = payload.allowed_actions or tuple(sorted(REVIEW_ACTIONS))
            review_context = ImprovementReviewContext(
                improvement_id=payload.projection.improvement_id,
                expected_improvement_version=payload.projection.version,
                state=payload.projection.state,
                kind=payload.projection.kind,
                delivery=payload.projection.delivery,
                source_refs=payload.projection.source_refs,
                target=payload.projection.target,
                proposed_change=payload.projection.proposed_change,
                verification=payload.projection.verification,
                risk=payload.projection.risk,
                confidence=payload.projection.confidence,
                allowed_actions=allowed,
                advice=ImprovementReviewAdvice(delivery=payload.projection.delivery),
            )
            return succeeded(cast(dict[str, object], review_context.model_dump(mode="json")))
        except InputError as error:
            return failed_input(error)


class ApplyImprovementReviewHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            payload = validate_input(ApplyReviewInput, request.input)
            updated = apply_review(payload)
            dumped = updated.model_dump(mode="json")
            return succeeded(
                {
                    **cast(dict[str, object], dumped),
                    "lifecycle_state": updated.state.value,
                    "effect_intents": [],
                    "write_authorization": [],
                }
            )
        except InputError as error:
            return failed_input(error)


__all__ = [
    "ApplyImprovementAutoReviewHandler",
    "ApplyImprovementReviewHandler",
    "LoadImprovementReviewContextHandler",
    "LoadReviewSubjectHandler",
    "RecordAutoReviewOrchestrationErrorHandler",
    "RecordImprovementAutoReviewErrorHandler",
    "SelectCurrentRetroAutoReviewItemsHandler",
    "SummarizeAutoReviewBatchHandler",
    "ValidateImprovementReviewAssessmentHandler",
    "apply_auto_review",
    "apply_review",
    "assert_improvement_transition",
]
