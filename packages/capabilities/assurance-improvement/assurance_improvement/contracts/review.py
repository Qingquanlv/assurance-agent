"""Improvement review subject, auto-review assessment, and batch summary."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from assurance_improvement.contracts.improvements import (
    DeliveryKind,
    ImprovementKind,
    ImprovementProjection,
    ImprovementSourceRefs,
    ImprovementVerification,
)
from assurance_improvement.contracts.retro import RetroPipelineFailure, RetroSourceManifestV3, Signal

_FROZEN = ConfigDict(frozen=True, extra="forbid")
_DIGEST = r"^sha256:[0-9a-f]{64}$"


class AutoReviewFinding(BaseModel):
    model_config = _FROZEN

    finding_id: str = Field(min_length=1)
    severity: Literal["info", "low", "medium", "high", "critical", "blocking"]
    category: str = Field(min_length=1)
    message: str = Field(min_length=1)
    source_refs: tuple[str, ...] = ()


class ImprovementAutoReviewAssessmentAuthoring(BaseModel):
    model_config = _FROZEN

    schema_version: Literal["1"] = "1"
    review_type: Literal["improvement"] = "improvement"
    decision: Literal["pass", "changes_requested", "needs_human_review", "reject"]
    findings: tuple[AutoReviewFinding, ...] = ()
    evidence_traceability: Literal["complete", "incomplete", "invalid"]
    scope_readiness: Literal["ready", "not_ready", "ambiguous"]
    verification_readiness: Literal["ready", "not_ready"]
    delivery_safety: Literal["ready", "not_ready", "needs_human_review"]
    human_review_required: bool


class ImprovementAutoReviewAssessment(ImprovementAutoReviewAssessmentAuthoring):
    review_id: str = Field(min_length=1)
    improvement_id: str = Field(min_length=1)
    expected_improvement_version: int = Field(ge=1)
    subject_sha256: str = Field(pattern=_DIGEST)


class ImprovementReviewProvenance(BaseModel):
    model_config = _FROZEN

    retro_id: str = Field(min_length=1)
    candidate_id: str = Field(min_length=1)
    context_sha256: str = Field(pattern=_DIGEST)
    candidate_batch_digest: str = Field(pattern=_DIGEST)


class ImprovementReviewSubject(BaseModel):
    model_config = _FROZEN

    schema_version: Literal["1"] = "1"
    improvement_id: str = Field(min_length=1)
    kind: ImprovementKind
    delivery: DeliveryKind
    target: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    proposed_change: str = Field(min_length=1)
    verification: ImprovementVerification
    risk: Literal["low", "medium", "high"]
    confidence: Literal["low", "medium", "high"]
    source_refs: ImprovementSourceRefs
    signal_evidence: tuple[Signal, ...]
    source_manifest: RetroSourceManifestV3
    pipeline_failures: tuple[RetroPipelineFailure, ...] = ()
    provenance: ImprovementReviewProvenance


class ImprovementAutoReviewStatus(BaseModel):
    model_config = _FROZEN

    schema_version: Literal["1"] = "1"
    review_id: str = Field(min_length=1)
    improvement_id: str = Field(min_length=1)
    result: Literal["approved", "escalated", "review_error", "stale"]
    ledger_event_id: str | None = None
    replayed: bool = False


AutoReviewRoute = Literal["apply-evaluate", "rework", "rejected", "human-review", "failed"]
HumanReviewRoute = Literal["apply-evaluate", "rejected", "rework", "superseded", "failed"]

_AUTO_REVIEW_ROUTE: dict[str, AutoReviewRoute] = {
    "approved": "apply-evaluate",
    "needs_rework": "rework",
    "rejected": "rejected",
    "proposed": "human-review",
}
_HUMAN_REVIEW_ROUTE: dict[str, HumanReviewRoute] = {
    "approved": "apply-evaluate",
    "rejected": "rejected",
    "needs_rework": "rework",
    "superseded": "superseded",
}
# Projection states that used to fall through to the graph's otherwise target.
AUTO_REVIEW_FAILED_STATES = frozenset(
    {
        "evaluating",
        "exported",
        "applied",
        "rolled_back",
        "awaiting_baseline",
        "eval_error",
        "superseded",
    }
)
HUMAN_REVIEW_FAILED_STATES = frozenset(
    {
        "proposed",
        "evaluating",
        "exported",
        "applied",
        "rolled_back",
        "awaiting_baseline",
        "eval_error",
    }
)


def projection_state_name(projection: object) -> str:
    if isinstance(projection, Mapping):
        raw = projection.get("state")
    else:
        raw = getattr(projection, "state", None)
    named = getattr(raw, "value", raw)
    return "" if named is None else str(named)


def auto_review_route(state: str) -> AutoReviewRoute:
    return _AUTO_REVIEW_ROUTE.get(state, "failed")


def human_review_route(state: str) -> HumanReviewRoute:
    return _HUMAN_REVIEW_ROUTE.get(state, "failed")


class AppliedAutoReviewV1(BaseModel):
    model_config = _FROZEN

    status: ImprovementAutoReviewStatus
    projection: ImprovementProjection
    route: AutoReviewRoute

    @model_validator(mode="before")
    @classmethod
    def _fill_route(cls, value: object) -> object:
        if not isinstance(value, dict) or "route" in value or "projection" not in value:
            return value
        return {**value, "route": auto_review_route(projection_state_name(value["projection"]))}


class ApplyReviewPublishedV1(BaseModel):
    model_config = _FROZEN

    projection: ImprovementProjection
    route: HumanReviewRoute

    @model_validator(mode="before")
    @classmethod
    def _fill_route(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        body = cast(dict[str, object], dict(value))
        projection = body.get("projection")
        if projection is None and "state" in body:
            projection = {key: item for key, item in body.items() if key != "route"}
            body = {"projection": projection}
            if "route" in value:
                body["route"] = value["route"]
        if "route" not in body and projection is not None:
            body["route"] = cast(Any, human_review_route(projection_state_name(projection)))
        return body


class AutoReviewBatchError(BaseModel):
    model_config = _FROZEN

    stage: Literal["selector", "fan_out", "summarize"]
    error_kind: str = Field(min_length=1)


class ImprovementAutoReviewBatchSummary(BaseModel):
    model_config = _FROZEN

    schema_version: Literal["1"] = "1"
    retro_id: str = Field(min_length=1)
    review_ids: tuple[str, ...]
    approved: int = Field(ge=0)
    escalated: int = Field(ge=0)
    errors: int = Field(ge=0)
    stale: int = Field(ge=0)
    orchestration_errors: tuple[AutoReviewBatchError, ...] = ()

    @model_validator(mode="after")
    def validate_child_counts(self) -> Self:
        if self.approved + self.escalated + self.errors + self.stale != len(self.review_ids):
            raise ValueError("semantic result counts must equal review_ids length")
        if len(set(self.review_ids)) != len(self.review_ids):
            raise ValueError("review_ids must be unique")
        return self
