"""Strict append-only event adapters for the Change Issue and Project Problem Ledgers.

Both ledgers are JSONL files.  Every line is a JSON object containing the
envelope fields (schema_version, seq, event_id, idempotency_key, ts,
evidence_digest) and the typed event payload identified by ``type``.

Change Issue events are scoped to one Change; Project Problem events are
project-wide.  Both adapters use ``extra="forbid"`` discriminated unions: an
unknown ``type`` value or an undeclared field raises ``ValidationError`` and
the whole read is rejected as a ledger integrity failure.

Public API (all other names are implementation details):
    CHANGE_ISSUE_EVENT_ADAPTER
    PROBLEM_EVENT_ADAPTER
    read_change_issue_events(path) -> list[ChangeIssueEvent]
    read_problem_events(path) -> list[ProblemEvent]
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from assurance_agent.artifacts.models.common import NonEmptyStr
from assurance_agent.artifacts.models.issues import (
    IssueAnalysisStatus,
    IssueClassification,
    IssueSeverity,
    IssueOccurrence,
    Observation,
    ProblemFingerprint,
)
from assurance_agent.exceptions import AaError


class LedgerIntegrityError(AaError):
    """Raised when a ledger file fails strict validation during a read."""


# ---------------------------------------------------------------------------
# Envelope base classes
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Change Issue Events
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Project Problem Events
# ---------------------------------------------------------------------------


class ProblemDetectedEvent(_BaseProblemEvent):
    """First occurrence of a fingerprint; creates a new Problem at version 1."""

    type: Literal["problem_detected"]
    expected_problem_version: Literal[0] = 0
    occurrence_id: NonEmptyStr
    change_id: NonEmptyStr
    batch_id: NonEmptyStr
    fingerprint: ProblemFingerprint
    title: NonEmptyStr
    classification: IssueClassification
    severity: IssueSeverity
    root_cause_hypothesis: NonEmptyStr | None = None


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


# ---------------------------------------------------------------------------
# Strict JSONL readers
# ---------------------------------------------------------------------------


def _read_strict(
    path: Path,
    adapter: TypeAdapter,  # type: ignore[type-arg]
) -> list:
    """Shared strict reader: blank holes, bad JSON, bad seq, duplicate IDs all fail."""
    if not path.exists():
        return []

    events: list = []
    seen_event_ids: set[str] = set()
    seen_idempotency_keys: set[str] = set()
    expected_seq = 1

    for line_no, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not raw_line.strip():
            raise LedgerIntegrityError(
                f"{path} line {line_no}: blank hole in ledger"
            )

        try:
            data = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise LedgerIntegrityError(
                f"{path} line {line_no}: invalid JSON: {exc}"
            ) from exc

        if not isinstance(data, dict):
            raise LedgerIntegrityError(
                f"{path} line {line_no}: event is not a JSON object"
            )

        seq = data.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool) or seq != expected_seq:
            raise LedgerIntegrityError(
                f"{path} line {line_no}: expected seq {expected_seq}, got {seq!r}"
            )

        try:
            event = adapter.validate_python(data)
        except ValidationError as exc:
            raise LedgerIntegrityError(
                f"{path} line {line_no}: invalid event: {exc}"
            ) from exc

        event_id: str = event.event_id
        idempotency_key: str = event.idempotency_key

        if event_id in seen_event_ids:
            raise LedgerIntegrityError(
                f"{path} line {line_no}: duplicate event_id {event_id!r}"
            )
        if idempotency_key in seen_idempotency_keys:
            raise LedgerIntegrityError(
                f"{path} line {line_no}: duplicate idempotency_key {idempotency_key!r}"
            )

        seen_event_ids.add(event_id)
        seen_idempotency_keys.add(idempotency_key)
        events.append(event)
        expected_seq += 1

    return events


def read_change_issue_events(path: Path) -> list[ChangeIssueEvent]:
    """Read and strictly validate a Change Issue events.jsonl file."""
    return _read_strict(path, CHANGE_ISSUE_EVENT_ADAPTER)  # type: ignore[return-value]


def read_problem_events(path: Path) -> list[ProblemEvent]:
    """Read and strictly validate a Project Problem events.jsonl file."""
    return _read_strict(path, PROBLEM_EVENT_ADAPTER)  # type: ignore[return-value]
