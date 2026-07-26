"""Issue lifecycle artifact models (versioned).

Canonical Change-relative inspect/** and issues/snapshot.json contracts for
Observation collection, LLM candidate analysis, reconciliation status, and
Change Issue projections. Project-level qa/issues/*.json models live here but
are validated by the Issue store, not the Change-relative artifact registry.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from assurance_agent.artifacts.models.common import NonEmptyStr

ObservationKind = Literal[
    "test_failure",
    "warning",
    "anomaly",
    "workaround",
    "coverage_gap",
    "performance_signal",
    "environment_signal",
    "review_finding",
]
IssueClassification = Literal[
    "product_bug",
    "test_bug",
    "test_data_issue",
    "environment_issue",
    "coverage_gap",
    "performance_issue",
    "workflow_issue",
    "unknown",
]
IssueSeverity = Literal["critical", "high", "medium", "low"]
ProblemStatus = Literal[
    "detected",
    "triaged",
    "in_progress",
    "verification_pending",
    "resolved",
    "not_an_issue",
    "accepted_risk",
]
AssessmentAuthority = Literal["llm_provisional", "human_confirmed"]
ObservationTarget = Literal["api", "e2e", "fuzz", "performance", "coverage"]
AffectedSurfaceKind = Literal[
    "endpoint",
    "module",
    "case",
    "test",
    "workflow",
    "environment",
    "unknown",
]
IssueAnalysisStatusValue = Literal["completed", "pending", "failed"]
IssueAnalysisFailureReason = Literal["timeout", "transport", "invalid_output", "unavailable"]
IssueReconcileStatusValue = Literal["completed", "failed"]
ProjectSyncStatus = Literal["completed", "pending"]

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class ObservationSource(BaseModel):
    model_config = _FROZEN

    artifact: NonEmptyStr
    json_pointer: NonEmptyStr


class Observation(BaseModel):
    model_config = _FROZEN

    observation_id: NonEmptyStr
    change_id: NonEmptyStr
    batch_id: NonEmptyStr
    kind: ObservationKind
    target: ObservationTarget
    case_id: NonEmptyStr | None = None
    source: ObservationSource
    evidence_refs: list[NonEmptyStr] = Field(min_length=1)
    signature: NonEmptyStr
    observed_at: NonEmptyStr


class ObservationDocument(BaseModel):
    model_config = _FROZEN

    schema_version: Literal["1.0"]
    change_id: NonEmptyStr
    batch_id: NonEmptyStr
    observations: list[Observation]


class IssueEvidenceManifestEntry(BaseModel):
    model_config = _FROZEN

    path: NonEmptyStr
    digest: NonEmptyStr


class IssueEvidenceManifest(BaseModel):
    model_config = _FROZEN

    schema_version: Literal["1.0"]
    change_id: NonEmptyStr
    batch_id: NonEmptyStr
    digest: NonEmptyStr
    entries: list[IssueEvidenceManifestEntry] = Field(min_length=1)


class IssueCandidateProposed(BaseModel):
    model_config = _FROZEN

    title: NonEmptyStr
    classification: IssueClassification
    severity: IssueSeverity
    root_cause_hypothesis: NonEmptyStr


class AffectedSurface(BaseModel):
    model_config = _FROZEN

    kind: AffectedSurfaceKind
    value: NonEmptyStr


class FingerprintInputs(BaseModel):
    model_config = _FROZEN

    surface: NonEmptyStr
    symptom: NonEmptyStr
    qualifiers: list[NonEmptyStr] | None = None


class IssueCandidate(BaseModel):
    model_config = _FROZEN

    candidate_id: NonEmptyStr
    observation_ids: list[NonEmptyStr] = Field(min_length=1)
    proposed: IssueCandidateProposed
    affected_surface: AffectedSurface
    fingerprint_inputs: FingerprintInputs
    possible_problem_ids: list[NonEmptyStr]
    confidence: float = Field(ge=0.0, le=1.0)
    recommended_action: NonEmptyStr


class IssueCandidateDocument(BaseModel):
    model_config = _FROZEN

    schema_version: Literal["1.0"]
    change_id: NonEmptyStr
    batch_id: NonEmptyStr
    evidence_bundle_digest: NonEmptyStr
    candidates: list[IssueCandidate]


class IssueAnalysisStatus(BaseModel):
    model_config = _FROZEN

    schema_version: Literal["1.0"]
    change_id: NonEmptyStr
    batch_id: NonEmptyStr
    status: IssueAnalysisStatusValue
    evidence_bundle_digest: NonEmptyStr
    candidate_count: int = Field(ge=0)
    reason: IssueAnalysisFailureReason | None = None
    retryable: bool | None = None
    candidate_digest: NonEmptyStr | None = None


class IssueReconcileStatus(BaseModel):
    model_config = _FROZEN

    schema_version: Literal["1.0"]
    change_id: NonEmptyStr
    batch_id: NonEmptyStr
    status: IssueReconcileStatusValue
    evidence_bundle_digest: NonEmptyStr
    candidate_digest: NonEmptyStr | None = None
    occurrence_count: int | None = Field(default=None, ge=0)
    error: NonEmptyStr | None = None


class ProvisionalAssessment(BaseModel):
    model_config = _FROZEN

    classification: IssueClassification
    severity: IssueSeverity
    authority: AssessmentAuthority
    root_cause_hypothesis: NonEmptyStr


class OccurrenceAnalysis(BaseModel):
    model_config = _FROZEN

    evidence_bundle_digest: NonEmptyStr
    analyzer: NonEmptyStr
    prompt_version: NonEmptyStr
    candidate_digest: NonEmptyStr


class IssueOccurrence(BaseModel):
    model_config = _FROZEN

    occurrence_id: NonEmptyStr
    change_id: NonEmptyStr
    batch_id: NonEmptyStr
    observation_ids: list[NonEmptyStr] = Field(min_length=1)
    problem_id: NonEmptyStr
    provisional_assessment: ProvisionalAssessment
    analysis: OccurrenceAnalysis


class ProblemFingerprint(BaseModel):
    model_config = _FROZEN

    version: Literal["1"]
    digest: NonEmptyStr


class ProblemAssessment(BaseModel):
    model_config = _FROZEN

    classification: IssueClassification
    severity: IssueSeverity
    authority: AssessmentAuthority
    root_cause_hypothesis: NonEmptyStr | None = None


class ProblemSeenRef(BaseModel):
    model_config = _FROZEN

    change_id: NonEmptyStr
    occurrence_id: NonEmptyStr


class ProblemResolution(BaseModel):
    model_config = _FROZEN

    resolved_at: NonEmptyStr
    change_id: NonEmptyStr
    batch_id: NonEmptyStr
    disposition: NonEmptyStr
    verification_scope: list[NonEmptyStr] = Field(min_length=1)
    evidence_digest: NonEmptyStr


class ProblemVerificationRequest(BaseModel):
    model_config = _FROZEN

    requested_at: NonEmptyStr
    change_id: NonEmptyStr
    batch_id: NonEmptyStr
    linked_fix_disposition: NonEmptyStr
    verification_scope: list[NonEmptyStr] = Field(min_length=1)
    evidence_digest: NonEmptyStr


class Problem(BaseModel):
    model_config = _FROZEN

    problem_id: NonEmptyStr
    fingerprint: ProblemFingerprint
    title: NonEmptyStr
    assessment: ProblemAssessment
    status: ProblemStatus
    first_seen: ProblemSeenRef
    last_seen: ProblemSeenRef
    occurrences: list[NonEmptyStr] = Field(min_length=1)
    verification_request: ProblemVerificationRequest | None = None
    resolution: ProblemResolution | None = None
    version: int = Field(ge=1)

    @model_validator(mode="after")
    def _resolved_requires_resolution(self) -> "Problem":
        if self.status == "resolved" and self.resolution is None:
            raise ValueError("resolution is required when status is resolved")
        return self


class ChangeIssueSnapshot(BaseModel):
    model_config = _FROZEN

    schema_version: Literal["1.0"]
    change_id: NonEmptyStr
    authoritative_batch_id: NonEmptyStr
    observations: list[Observation]
    occurrences: list[IssueOccurrence]
    analysis_status: IssueAnalysisStatus | None = None
    project_sync_status: ProjectSyncStatus = "completed"
    batches: list[NonEmptyStr] = Field(min_length=1)


class ProblemProjection(BaseModel):
    model_config = _FROZEN

    schema_version: Literal["1.0"]
    generated_at: NonEmptyStr
    problems: list[Problem]


class ProblemReviewQueueEntry(BaseModel):
    model_config = _FROZEN

    entry_id: NonEmptyStr
    change_id: NonEmptyStr
    occurrence_id: NonEmptyStr
    candidate_id: NonEmptyStr | None = None
    possible_problem_ids: list[NonEmptyStr] = Field(min_length=1)
    reason: NonEmptyStr
    created_at: NonEmptyStr


class ProblemReviewQueue(BaseModel):
    model_config = _FROZEN

    schema_version: Literal["1.0"]
    entries: list[ProblemReviewQueueEntry]
