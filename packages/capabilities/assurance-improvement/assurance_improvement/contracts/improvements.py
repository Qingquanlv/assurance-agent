"""Improvement lifecycle, source-reference, and projection contracts."""

from __future__ import annotations

from enum import StrEnum
from itertools import chain
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from assurance_improvement.contracts.knowledge import DataKnowledgeProposal, PersistedDataKnowledgeProposal
from assurance_intake.contracts import NonEmptyStr

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class ImprovementKind(StrEnum):
    PROMPT = "prompt_improvement"
    FIXTURE = "fixture_improvement"
    TEST = "test_improvement"
    WORKFLOW = "workflow_improvement"
    DOMAIN_KNOWLEDGE = "domain_knowledge"


class DeliveryKind(StrEnum):
    MEMORY_PATCH = "memory_patch"
    CHANGE_DRAFT = "change_draft"
    KNOWLEDGE_DELTA = "knowledge_delta"
    TEST_PROMOTION = "test_promotion"


class ImprovementState(StrEnum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"
    NEEDS_REWORK = "needs_rework"
    EVALUATING = "evaluating"
    EXPORTED = "exported"
    APPLIED = "applied"
    ROLLED_BACK = "rolled_back"
    AWAITING_BASELINE = "awaiting_baseline"
    EVAL_ERROR = "eval_error"
    SUPERSEDED = "superseded"


ALLOWED_DELIVERIES: dict[ImprovementKind, frozenset[DeliveryKind]] = {
    ImprovementKind.PROMPT: frozenset({DeliveryKind.MEMORY_PATCH}),
    ImprovementKind.FIXTURE: frozenset(
        {DeliveryKind.MEMORY_PATCH, DeliveryKind.CHANGE_DRAFT, DeliveryKind.TEST_PROMOTION}
    ),
    ImprovementKind.TEST: frozenset(
        {DeliveryKind.MEMORY_PATCH, DeliveryKind.CHANGE_DRAFT, DeliveryKind.TEST_PROMOTION}
    ),
    ImprovementKind.WORKFLOW: frozenset({DeliveryKind.CHANGE_DRAFT}),
    ImprovementKind.DOMAIN_KNOWLEDGE: frozenset({DeliveryKind.KNOWLEDGE_DELTA}),
}


def is_valid_memory_patch_target(target: str) -> bool:
    if target.startswith("/") or "\\" in target or "\x00" in target:
        return False
    parts = target.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        return False
    return len(parts) > 2 and parts[:2] == [".aa", "memory"]


class ImprovementVerification(BaseModel):
    model_config = _FROZEN

    suites: tuple[str, ...] = ()
    required_cases: tuple[str, ...] = ()
    success_criteria: str = Field(min_length=1)


class ImprovementSourceRefs(BaseModel):
    model_config = _FROZEN

    problem_ids: tuple[str, ...] = ()
    occurrence_ids: tuple[str, ...] = ()
    issue_event_ids: tuple[str, ...] = ()
    workflow_evidence_ids: tuple[str, ...] = ()
    eval_run_ids: tuple[str, ...] = ()

    def all_ids(self) -> tuple[str, ...]:
        return tuple(sorted(set(chain.from_iterable(self.model_dump().values()))))


class ImprovementCandidate(BaseModel):
    model_config = _FROZEN

    candidate_id: str = Field(min_length=1)
    kind: ImprovementKind
    delivery: DeliveryKind
    source_refs: ImprovementSourceRefs
    target: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    proposed_change: str = Field(min_length=1)
    knowledge_delta: DataKnowledgeProposal | None = None
    verification: ImprovementVerification
    risk: Literal["low", "medium", "high"]
    confidence: Literal["low", "medium", "high"]
    supersedes: str | None = None

    @model_validator(mode="after")
    def validate_delivery(self) -> Self:
        if self.delivery not in ALLOWED_DELIVERIES[self.kind]:
            raise ValueError(f"{self.kind} cannot use {self.delivery}")
        if self.delivery is DeliveryKind.MEMORY_PATCH and not is_valid_memory_patch_target(self.target):
            raise ValueError("memory_patch target must be a child path under .aa/memory/")
        if not self.source_refs.all_ids():
            raise ValueError("candidate requires at least one source ref")
        if (self.knowledge_delta is not None) != (self.delivery is DeliveryKind.KNOWLEDGE_DELTA):
            raise ValueError("knowledge_delta payload is required only for knowledge_delta delivery")
        return self


class LastAutoReview(BaseModel):
    model_config = _FROZEN

    review_id: str = Field(min_length=1)
    subject_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    assessment_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    policy_version: str = Field(min_length=1)
    verdict: Literal[
        "auto_approved",
        "changes_requested",
        "needs_human_review",
        "reject_advice",
        "review_error",
    ]


class ImprovementProjection(BaseModel):
    model_config = _FROZEN

    improvement_id: str
    fingerprint: str
    fingerprint_version: Literal["1"] = "1"
    kind: ImprovementKind
    delivery: DeliveryKind
    source_refs: ImprovementSourceRefs
    target: str
    rationale: str
    proposed_change: str
    knowledge_delta: PersistedDataKnowledgeProposal | None = None
    verification: ImprovementVerification
    risk: Literal["low", "medium", "high"]
    confidence: Literal["low", "medium", "high"]
    state: ImprovementState
    version: int = Field(ge=1)
    proposed_by_retro_ids: tuple[str, ...]
    supersedes: str | None = None
    last_event_id: str
    review_subject_sha256: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    approval_source: Literal["none", "human", "automatic"] = "none"
    last_auto_review: LastAutoReview | None = None


class ImprovementLedgerProjection(BaseModel):
    model_config = _FROZEN

    schema_version: Literal["1"] = "1"
    last_seq: int = Field(ge=0)
    improvements: dict[str, ImprovementProjection]
    by_fingerprint: dict[str, str]


class ReconcileResultV1(ImprovementLedgerProjection):
    improvement_ids: tuple[str, ...]
    events: tuple[dict[str, str | int], ...]


class ImprovementReviewQueue(BaseModel):
    model_config = _FROZEN

    schema_version: Literal["1"] = "1"
    improvement_ids: tuple[str, ...]


class ImprovementReviewAdvice(BaseModel):
    model_config = _FROZEN

    delivery: DeliveryKind
    checklist: tuple[str, ...] = ()
    memory_patch_path: str | None = None
    change_draft_outline: str | None = None
    knowledge_delta_summary: str | None = None


class ImprovementReviewContext(BaseModel):
    model_config = _FROZEN

    improvement_id: str = Field(min_length=1)
    expected_improvement_version: int = Field(ge=1)
    state: ImprovementState
    kind: ImprovementKind
    delivery: DeliveryKind
    source_refs: ImprovementSourceRefs
    target: str = Field(min_length=1)
    proposed_change: str = Field(min_length=1)
    verification: ImprovementVerification
    risk: Literal["low", "medium", "high"]
    confidence: Literal["low", "medium", "high"]
    allowed_actions: tuple[str, ...]
    advice: ImprovementReviewAdvice


class ImprovementCandidateDocument(BaseModel):
    model_config = _FROZEN

    schema_version: Literal["2"] = "2"
    retro_id: str
    context_sha256: str
    candidates: tuple[ImprovementCandidate, ...] = ()


class ImprovementCandidateV3(ImprovementCandidate):
    signal_ids: tuple[NonEmptyStr, ...] = Field(min_length=1)
