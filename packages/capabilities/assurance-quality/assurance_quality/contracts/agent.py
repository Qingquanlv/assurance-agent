"""Private prepare/finalize request and result models owned by assurance-quality."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any, Literal, Self

from pydantic import Field, field_validator, model_validator

from agent_runtime_contracts import AgentRunResult
from graph_engine.attempts.models.resolutions import ReceiptRef
from graph_engine.plugin_api import FrozenModel

from assurance_quality.contracts.baseline import _reject_endpoint_inventories
from assurance_quality.contracts.issues import IssueCandidate, IssueTriageAdvice
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1

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


class QualitySkillInputV1(FrozenModel):
    change_id: str = Field(min_length=1)
    batch_id: str = Field(min_length=1)
    capability_leafs: tuple[str, ...]
    artifact_paths: tuple[str, ...]
    owned_evidence_ids: tuple[str, ...] = ()
    evidence_bundle_digest: str | None = Field(default=None, pattern=_DIGEST_REF)
    execution_digest: str = Field(pattern=_SHA256)
    healing_digest: str | None = Field(default=None, pattern=_SHA256)
    trace_digest: str = Field(pattern=_SHA256)
    coverage_digest: str = Field(pattern=_SHA256)
    metrics_digest: str = Field(pattern=_SHA256)
    case_digest: str = Field(pattern=_SHA256)
    plan_digest: str = Field(pattern=_SHA256)
    plan_ref: EvidenceArtifactRefV1
    mapping_digest: str = Field(pattern=_SHA256)
    issue_digest: str | None = Field(default=None, pattern=_SHA256)
    validation_error: str | None = Field(default=None, min_length=1, max_length=8192)
    # Tail-only. Absent on the issue-review thin entry, so the graph table applies.
    inspection_disposition: str | None = None

    @field_validator("capability_leafs")
    @classmethod
    def _leafs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _sorted_unique(value, label="capability leaf")

    @field_validator("artifact_paths")
    @classmethod
    def _paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _canonical_relative_paths(value)

    @field_validator("owned_evidence_ids")
    @classmethod
    def _evidence(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _sorted_unique(value, label="owned evidence") if value else ()


class IssueAnalysisBoundInputV1(FrozenModel):
    """Scalars a thin entry already has, or refs prepare opens into the skill."""

    change_id: str | None = None
    batch_id: str | None = None
    capability_leafs: tuple[str, ...] = ()
    artifact_paths: tuple[str, ...] = ()
    allowed_artifact_paths: tuple[str, ...] = ()
    owned_evidence_ids: tuple[str, ...] = ()
    evidence_bundle_digest: str | None = None
    execution_evidence_digest: str | None = None
    healing_digest: str | None = None
    trace_digest: str | None = None
    coverage_digest: str | None = None
    metrics_digest: str | None = None
    case_digest: str | None = None
    plan_digest: str | None = None
    plan_ref: EvidenceArtifactRefV1 | None = None
    mapping_digest: str | None = None
    issue_digest: str | None = None
    inspection_disposition: str | None = None
    rounds_budget: int = 0
    rounds_used: int = 0
    coverage_epoch: int = 0
    inspection_ref: EvidenceArtifactRefV1 | None = None
    assessment_ref: EvidenceArtifactRefV1 | None = None
    generation_ref: EvidenceArtifactRefV1 | None = None
    inspection_receipt: ReceiptRef | None = None
    validation_error: str | None = Field(default=None, min_length=1, max_length=8192)


class ReconcileBoundInputV1(IssueAnalysisBoundInputV1):
    issue_analysis_ref: EvidenceArtifactRefV1 | None = None


class IssueAnalysisSkillInputV1(QualitySkillInputV1):
    """Issue analysis may read the inspection outcome and assessment from the ledger."""

    inspection_ref: EvidenceArtifactRefV1 | None = None
    assessment_ref: EvidenceArtifactRefV1 | None = None
    coverage_epoch: int = Field(default=0, ge=0)


class AgentFinalizeInputV1(QualitySkillInputV1):
    agent_result: AgentRunResult
    locked_evidence_digests: dict[str, str] = Field(default_factory=dict)

    @field_validator("locked_evidence_digests")
    @classmethod
    def _locked_digests(cls, value: dict[str, str]) -> dict[str, str]:
        cleaned: dict[str, str] = {}
        for path, digest in value.items():
            posix = PurePosixPath(path)
            if posix.is_absolute() or any(part in {"", ".", ".."} for part in posix.parts):
                raise ValueError("evidence path must be canonical and relative")
            token = digest.strip()
            if not token:
                raise ValueError("locked evidence digest must be non-empty")
            cleaned[path] = token
        return dict(sorted(cleaned.items()))


class FactBaselineResultV1(FrozenModel):
    source: Literal["seed_file", "db_probe", "both", "unavailable"]
    schema_version: str | None = None
    change_id: str | None = None
    warnings: tuple[str, ...] = ()
    facts: dict[str, Any] | None = None
    seed_file: str | None = None
    source_evidence_ids: tuple[str, ...] = ()

    @field_validator("source_evidence_ids")
    @classmethod
    def _ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _sorted_unique(value, label="source evidence") if value else ()

    @model_validator(mode="after")
    def _authoring_rules(self) -> FactBaselineResultV1:
        _reject_endpoint_inventories(self.facts)
        if self.source != "unavailable" and not self.schema_version:
            raise ValueError("schema_version is required when facts are available")
        return self


class InspectionResultV1(FrozenModel):
    schema_version: Literal["1.0"]
    change_id: str = Field(min_length=1)
    batch_id: str = Field(min_length=1)
    inspect_mode: Literal["primary"]
    classification_performed: bool
    status: Literal["analyzed", "no_failures", "failed"]
    execution_digest: str = Field(pattern=_SHA256)
    healing_digest: str | None = Field(default=None, pattern=_SHA256)
    trace_digest: str = Field(pattern=_SHA256)
    coverage_digest: str = Field(pattern=_SHA256)
    metrics_digest: str = Field(pattern=_SHA256)


class IssueAnalysisResultV1(FrozenModel):
    schema_version: Literal["1.0"]
    change_id: str = Field(min_length=1)
    batch_id: str = Field(min_length=1)
    evidence_bundle_digest: str = Field(pattern=_DIGEST_REF)
    status: Literal["completed", "pending", "failed"]
    candidate_count: int = Field(ge=0)
    candidates: tuple[IssueCandidate, ...] = ()
    reason: str | None = None

    @model_validator(mode="after")
    def _count_matches(self) -> IssueAnalysisResultV1:
        if self.candidate_count != len(self.candidates):
            raise ValueError("candidate_count must equal the number of candidates")
        ids = [candidate.candidate_id for candidate in self.candidates]
        if len(ids) != len(set(ids)):
            raise ValueError("issue candidate ids must be unique")
        return self

    def require_complete_coverage(self, owned: set[str] | frozenset[str]) -> None:
        covered = {identifier for candidate in self.candidates for identifier in candidate.observation_ids}
        if not covered <= owned:
            raise ValueError("issue candidate cites unowned evidence")
        if self.status == "completed" and (not owned or covered != owned):
            raise ValueError("completed issue analysis must cover every owned observation")


class FinalizedIssueAnalysisV1(FrozenModel):
    agent_result: IssueAnalysisResultV1
    candidate_digest: str | None = Field(default=None, pattern=_DIGEST_REF)
    issue_analysis_ref: EvidenceArtifactRefV1

    @model_validator(mode="after")
    def _closed_result(self) -> Self:
        expected = "qa/results/inspect/issue-analysis.json"
        if self.issue_analysis_ref.path != expected:
            raise ValueError("issue analysis ref must use the current change path")
        if self.agent_result.status == "completed" and self.candidate_digest is None:
            raise ValueError("completed issue analysis requires candidate_digest")
        if self.agent_result.status != "completed" and self.candidate_digest is not None:
            raise ValueError("non-completed issue analysis cannot publish candidate_digest")
        return self


class IssueTriageResultV1(IssueTriageAdvice):
    """Typed issue-triage advice; finalize authenticates echoed evidence digests."""


class ReportResultV1(FrozenModel):
    schema_version: Literal["1.1"]
    change_id: str = Field(min_length=1)
    batch_id: str = Field(min_length=1)
    purpose: Literal["normal", "diagnostic"]
    report_files: tuple[str, ...] = Field(min_length=1)
    case_digest: str = Field(pattern=_SHA256)
    plan_digest: str = Field(pattern=_SHA256)
    mapping_digest: str = Field(pattern=_SHA256)
    execution_digest: str = Field(pattern=_SHA256)
    healing_digest: str | None = Field(pattern=_SHA256)
    trace_digest: str = Field(pattern=_SHA256)
    coverage_digest: str = Field(pattern=_SHA256)
    issue_digest: str | None = Field(pattern=_SHA256)
    metrics_digest: str = Field(pattern=_SHA256)
    risk_rationale: str | None = None
    recommendation: str | None = None

    @field_validator("report_files")
    @classmethod
    def _report_files(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _canonical_relative_paths(value)


__all__ = [
    "AgentFinalizeInputV1",
    "FactBaselineResultV1",
    "FinalizedIssueAnalysisV1",
    "InspectionResultV1",
    "IssueAnalysisResultV1",
    "IssueTriageResultV1",
    "QualitySkillInputV1",
    "ReportResultV1",
]
