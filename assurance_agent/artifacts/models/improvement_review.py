"""Strict artifacts for bounded automatic Improvement review."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from assurance_agent.artifacts.models.improvements import (
    DeliveryKind,
    ImprovementKind,
    ImprovementSourceRefs,
    ImprovementVerification,
)
from assurance_agent.artifacts.models.retro_batch import RetroPipelineFailure
from assurance_agent.artifacts.models.retro_v3 import RetroSourceManifestV3, Signal

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
    """Reviewer-authored judgment; runtime-owned identity is deliberately absent."""

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
    """Canonical assessment after runtime binds it to one immutable subject."""

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


__all__ = [
    "AutoReviewBatchError",
    "AutoReviewFinding",
    "ImprovementAutoReviewAssessment",
    "ImprovementAutoReviewAssessmentAuthoring",
    "ImprovementAutoReviewBatchSummary",
    "ImprovementAutoReviewStatus",
    "ImprovementReviewProvenance",
    "ImprovementReviewSubject",
]
