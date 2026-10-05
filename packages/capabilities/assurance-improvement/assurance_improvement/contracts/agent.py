"""Private prepare/finalize request and result models owned by assurance-improvement."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from agent_runtime_contracts import AgentRunResult
from assurance_intake.contracts import EvidenceArtifactRefV1
from graph_engine.plugin_api import FrozenModel

from assurance_quality.contracts.report import QualityReport

from assurance_improvement.contracts.improvements import ImprovementCandidateV3, ImprovementProjection
from assurance_improvement.contracts.retro import (
    EvalEvidenceSlice,
    IssueEvidenceSlice,
    RetroContextV3,
    RetroSourceManifestV3,
    Signal,
    WorkflowEvidenceSlice,
)
from assurance_improvement.contracts.review import AutoReviewFinding, ImprovementReviewSubject

_SHA256 = r"^[0-9a-f]{64}$"
_DIGEST_REF = r"^(?:sha256:)?[0-9a-f]{64}$"


def _sorted_unique(values: tuple[str, ...], *, label: str) -> tuple[str, ...]:
    cleaned = tuple(item.strip() for item in values)
    if any(not item for item in cleaned):
        raise ValueError(f"{label} must be a non-empty string")
    return tuple(sorted(set(cleaned)))


def _canonical_relative_paths(values: tuple[str, ...]) -> tuple[str, ...]:
    paths = _sorted_unique(values, label="artifact path")
    for path in paths:
        posix = PurePosixPath(path)
        if (
            posix.is_absolute()
            or "\\" in path
            or (len(path) >= 2 and path[1] == ":")
            or posix.as_posix() != path
            or any(part in {"", ".", ".."} for part in posix.parts)
        ):
            raise ValueError("artifact path must be canonical and relative")
    return paths


class RetroAnalysisInputV1(FrozenModel):
    change_id: str = Field(min_length=1)
    validation_error: str | None = Field(default=None, min_length=1, max_length=8192)
    evidence_slice_ref: EvidenceArtifactRefV1 | None = None
    evidence_slice: (
        Annotated[
            IssueEvidenceSlice | WorkflowEvidenceSlice | EvalEvidenceSlice,
            Field(discriminator="domain"),
        ]
        | None
    ) = None


class RetroSynthesisInputV1(FrozenModel):
    change_id: str = Field(min_length=1)
    validation_error: str | None = Field(default=None, min_length=1, max_length=8192)
    context_ref: EvidenceArtifactRefV1 | None = None
    context: RetroContextV3 | None = None


class RetroAnalysisFinalizeInputV1(RetroAnalysisInputV1):
    agent_result: AgentRunResult


class RetroSynthesisFinalizeInputV1(RetroSynthesisInputV1):
    agent_result: AgentRunResult


class ImprovementSkillInputV1(FrozenModel):
    change_id: str = Field(min_length=1)
    retro_id: str = Field(min_length=1)
    owned_evidence_ids: tuple[str, ...] = ()
    artifact_paths: tuple[str, ...] = ()
    source_manifest: RetroSourceManifestV3
    context_digest: str = Field(pattern=_SHA256)
    quality_report_digest: str = Field(pattern=_SHA256)
    metrics_digest: str = Field(pattern=_SHA256)
    issue_digest: str = Field(pattern=_SHA256)
    subject_digest: str = Field(pattern=_SHA256)
    expected_improvement_version: int = Field(ge=1)
    improvement_id: str = Field(min_length=1)
    invocation_id: str = Field(min_length=1)
    archive_digest: str = Field(pattern=_SHA256)
    locked_signal_ids: tuple[str, ...] = ()
    validation_error: str | None = Field(default=None, min_length=1, max_length=8192)

    @field_validator("owned_evidence_ids")
    @classmethod
    def _evidence(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _sorted_unique(value, label="owned evidence") if value else ()

    @field_validator("artifact_paths")
    @classmethod
    def _paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _canonical_relative_paths(value) if value else ()

    @field_validator("locked_signal_ids")
    @classmethod
    def _signals(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _sorted_unique(value, label="locked signal") if value else ()


class AgentFinalizeInputV1(ImprovementSkillInputV1):
    agent_result: AgentRunResult
    subject: ImprovementReviewSubject | None = None
    projection: ImprovementProjection | None = None
    quality_report: QualityReport | None = None


class RetroAnalysisResultV3(FrozenModel):
    schema_version: Literal["3"] = "3"
    retro_id: str = Field(min_length=1)
    domain: Literal["issue", "workflow", "eval", "discovery", "coverage_gap"] | None = None
    analysis_status: Literal["ok", "failed"] = "ok"
    failure_reason: str | None = None
    signals: tuple[Signal, ...] = ()
    candidates: tuple[ImprovementCandidateV3, ...] = ()

    @model_validator(mode="after")
    def _status_reason_consistency(self) -> RetroAnalysisResultV3:
        if self.analysis_status == "failed":
            if self.failure_reason is None:
                raise ValueError("failure_reason is required when analysis_status=failed")
            if self.signals or self.candidates:
                raise ValueError("failed analysis must not carry signals or candidates")
        elif self.failure_reason is not None:
            raise ValueError("failure_reason must be null when analysis_status=ok")
        return self


class ImprovementReviewResultV1(FrozenModel):
    schema_version: Literal["1"] = "1"
    review_type: Literal["improvement"] = "improvement"
    decision: Literal["pass", "changes_requested", "needs_human_review", "reject"]
    findings: tuple[AutoReviewFinding, ...] = ()
    evidence_traceability: Literal["complete", "incomplete", "invalid"]
    scope_readiness: Literal["ready", "not_ready", "ambiguous"]
    verification_readiness: Literal["ready", "not_ready"]
    delivery_safety: Literal["ready", "not_ready", "needs_human_review"]
    human_review_required: bool


class ReviewPublishedV1(FrozenModel):
    """Review fields the flow exports. ``result`` keeps the agent document."""

    decision: Literal["pass", "changes_requested", "needs_human_review", "reject"]
    human_review_required: bool
    lifecycle_state: str | None = None
    evidence_refs: tuple[dict[str, str], ...] = ()
    result: ImprovementReviewResultV1


class ArchiveResultV1(FrozenModel):
    schema_version: Literal["1"] = "1"
    change_id: str = Field(min_length=1)
    archive_status: Literal["archived", "archived_with_warnings"]
    issue_risk: Literal["unknown", "critical", "high", "medium", "low", "clear"] | None = None
    issue_risk_rationale: str | None = None
    summary: str = Field(min_length=1)
    artifact_paths: tuple[str, ...] = ()
    invocation_id: str = Field(min_length=1)
    archive_digest: str = Field(pattern=_SHA256)

    @field_validator("artifact_paths")
    @classmethod
    def _paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _canonical_relative_paths(value) if value else ()


class ArchivePublishedV1(FrozenModel):
    """Archive fields the flow exports. ``result`` keeps the agent document."""

    archive_status: Literal["archived", "archived_with_warnings"]
    lifecycle_state: str | None = None
    evidence_refs: tuple[dict[str, str], ...] = ()
    effect_refs: tuple[dict[str, str], ...] = ()
    result: ArchiveResultV1


__all__ = [
    "AgentFinalizeInputV1",
    "ArchivePublishedV1",
    "ArchiveResultV1",
    "ImprovementReviewResultV1",
    "ReviewPublishedV1",
    "ImprovementSkillInputV1",
    "RetroAnalysisResultV3",
]
