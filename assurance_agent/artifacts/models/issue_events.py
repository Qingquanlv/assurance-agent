"""Immutable Change Issue and Project Problem event wire schemas.

Pure Pydantic models and type adapters only. No filesystem or workflow imports.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from assurance_agent.artifacts.models.common import NonEmptyStr
from assurance_agent.artifacts.models.issues import (
    IssueAnalysisStatus,
    IssueClassification,
    IssueSeverity,
    IssueOccurrence,
    Observation,
    ProblemFingerprint,
)


class _BaseIssueEvent(BaseModel):
    """Common envelope fields shared by every Change Issue and Problem event."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    seq: int = Field(ge=1)
    event_id: NonEmptyStr
    idempotency_key: NonEmptyStr
    ts: NonEmptyStr
    evidence_digest: NonEmptyStr


class _BaseChangeIssueEvent(_BaseIssueEvent):
    change_id: NonEmptyStr
    batch_id: NonEmptyStr


class _BaseProblemEvent(_BaseIssueEvent):
    problem_id: NonEmptyStr
    expected_problem_version: int = Field(ge=0)


class ObservationRecordedEvent(_BaseChangeIssueEvent):
    type: Literal["observation_recorded"]
    observation: Observation


class IssueAnalysisCompletedEvent(_BaseChangeIssueEvent):
    type: Literal["issue_analysis_completed"]
    analysis_status: IssueAnalysisStatus


class IssueAnalysisFailedEvent(_BaseChangeIssueEvent):
    type: Literal["issue_analysis_failed"]
    analysis_status: IssueAnalysisStatus


class OccurrenceDetectedEvent(_BaseChangeIssueEvent):
    """New Problem was created for this Occurrence (no prior fingerprint match)."""

    type: Literal["occurrence_detected"]
    occurrence: IssueOccurrence


class OccurrenceLinkedEvent(_BaseChangeIssueEvent):
    """Occurrence was matched to an existing Problem by exact fingerprint."""

    type: Literal["occurrence_linked"]
    occurrence: IssueOccurrence


class ProjectSyncPendingEvent(_BaseChangeIssueEvent):
    """Reconcile write-set was not applied; idempotent retry is needed."""

    type: Literal["project_sync_pending"]
    candidate_digest: NonEmptyStr


ChangeIssueEvent = Annotated[
    ObservationRecordedEvent
    | IssueAnalysisCompletedEvent
    | IssueAnalysisFailedEvent
    | OccurrenceDetectedEvent
    | OccurrenceLinkedEvent
    | ProjectSyncPendingEvent,
    Field(discriminator="type"),
]

CHANGE_ISSUE_EVENT_ADAPTER: TypeAdapter[ChangeIssueEvent] = TypeAdapter(ChangeIssueEvent)


class ProblemDetectedEvent(_BaseProblemEvent):
    """First occurrence of a fingerprint; creates a new Problem at version 1."""

    type: Literal["problem_detected"]
    occurrence_id: NonEmptyStr
    change_id: NonEmptyStr
    batch_id: NonEmptyStr
    fingerprint: ProblemFingerprint
    title: NonEmptyStr
    classification: IssueClassification
    severity: IssueSeverity
    root_cause_hypothesis: NonEmptyStr | None = None

    @model_validator(mode="after")
    def _require_expected_version_zero(self) -> ProblemDetectedEvent:
        if self.expected_problem_version != 0:
            raise ValueError("problem_detected requires expected_problem_version == 0")
        return self


class ProblemOccurrenceLinkedEvent(_BaseProblemEvent):
    """Additional Occurrence was linked to an existing Problem."""

    type: Literal["problem_occurrence_linked"]
    occurrence_id: NonEmptyStr
    change_id: NonEmptyStr
    batch_id: NonEmptyStr


class ProblemAssessmentConfirmedEvent(_BaseProblemEvent):
    """Human confirmed or changed the problem classification/severity."""

    type: Literal["problem_assessment_confirmed"]
    classification: IssueClassification
    severity: IssueSeverity
    root_cause_hypothesis: NonEmptyStr | None = None
    reason: NonEmptyStr
    evidence_refs: list[NonEmptyStr] = Field(min_length=1)


class ProblemWorkStartedEvent(_BaseProblemEvent):
    type: Literal["problem_work_started"]
    reason: NonEmptyStr
    evidence_refs: list[NonEmptyStr] = Field(min_length=1)


class ProblemVerificationRequestedEvent(_BaseProblemEvent):
    type: Literal["problem_verification_requested"]
    verification_scope: list[NonEmptyStr] = Field(min_length=1)
    linked_fix_disposition: NonEmptyStr
    change_id: NonEmptyStr
    batch_id: NonEmptyStr


class ProblemResolvedEvent(_BaseProblemEvent):
    type: Literal["problem_resolved"]
    resolved_at: NonEmptyStr
    change_id: NonEmptyStr
    batch_id: NonEmptyStr
    disposition: NonEmptyStr
    verification_scope: list[NonEmptyStr] = Field(min_length=1)


class ProblemMarkedNotAnIssueEvent(_BaseProblemEvent):
    type: Literal["problem_marked_not_an_issue"]
    reason: NonEmptyStr
    evidence_refs: list[NonEmptyStr] = Field(min_length=1)


class ProblemRiskAcceptedEvent(_BaseProblemEvent):
    type: Literal["problem_risk_accepted"]
    reason: NonEmptyStr
    evidence_refs: list[NonEmptyStr] = Field(min_length=1)


class ProblemReopenedEvent(_BaseProblemEvent):
    type: Literal["problem_reopened"]
    reason: NonEmptyStr
    evidence_refs: list[NonEmptyStr] = Field(min_length=1)


class ProblemRegressedEvent(_BaseProblemEvent):
    type: Literal["problem_regressed"]
    occurrence_id: NonEmptyStr
    change_id: NonEmptyStr


class ProblemMergeSuggestedEvent(_BaseProblemEvent):
    """Semantic match found; a human must confirm before merge is applied."""

    type: Literal["problem_merge_suggested"]
    source_occurrence_id: NonEmptyStr
    source_change_id: NonEmptyStr
    target_problem_id: NonEmptyStr
    candidate_id: NonEmptyStr | None = None
    reason: NonEmptyStr


class ProblemMergedEvent(_BaseProblemEvent):
    """Source Problem is resolved as an alias of the target; history preserved."""

    type: Literal["problem_merged"]
    target_problem_id: NonEmptyStr
    reason: NonEmptyStr
    evidence_refs: list[NonEmptyStr] = Field(min_length=1)
    resolved_at: NonEmptyStr


ProblemEvent = Annotated[
    ProblemDetectedEvent
    | ProblemOccurrenceLinkedEvent
    | ProblemAssessmentConfirmedEvent
    | ProblemWorkStartedEvent
    | ProblemVerificationRequestedEvent
    | ProblemResolvedEvent
    | ProblemMarkedNotAnIssueEvent
    | ProblemRiskAcceptedEvent
    | ProblemReopenedEvent
    | ProblemRegressedEvent
    | ProblemMergeSuggestedEvent
    | ProblemMergedEvent,
    Field(discriminator="type"),
]

PROBLEM_EVENT_ADAPTER: TypeAdapter[ProblemEvent] = TypeAdapter(ProblemEvent)
