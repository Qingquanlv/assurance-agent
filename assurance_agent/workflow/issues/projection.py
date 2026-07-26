"""Pure deterministic projections over Change Issue and Project Problem events.

All functions are free of filesystem access and clock calls; callers supply
the event sequences (already validated) and receive frozen Pydantic models.

``dump_projection`` produces canonical JSON (compact separators, sorted keys,
trailing newline) that is byte-identical for the same model on every replay.

Public API:
    project_change_issues(events) -> ChangeIssueSnapshot
    project_problems(events) -> ProblemProjection
    project_review_queue(events) -> ProblemReviewQueue
    dump_projection(model) -> bytes
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import BaseModel

from assurance_agent.artifacts.models.issues import (
    AssessmentAuthority,
    ChangeIssueSnapshot,
    IssueAnalysisStatus,
    IssueClassification,
    IssueSeverity,
    IssueOccurrence,
    Observation,
    Problem,
    ProblemAssessment,
    ProblemFingerprint,
    ProblemProjection,
    ProblemResolution,
    ProblemVerificationRequest,
    ProblemReviewQueue,
    ProblemReviewQueueEntry,
    ProblemSeenRef,
    ProblemStatus,
    ProjectSyncStatus,
)

if TYPE_CHECKING:
    from assurance_agent.workflow.issues.events import (
        ChangeIssueEvent,
        ProblemEvent,
    )


class ProjectionError(Exception):
    """Raised when an event cannot be applied to the current projected state."""


# ---------------------------------------------------------------------------
# Internal mutable state for Problem projection
# ---------------------------------------------------------------------------


@dataclass
class _ProblemState:
    problem_id: str
    fingerprint: ProblemFingerprint
    title: str
    classification: IssueClassification
    severity: IssueSeverity
    authority: AssessmentAuthority
    root_cause_hypothesis: str | None
    status: ProblemStatus
    first_seen: ProblemSeenRef
    last_seen: ProblemSeenRef
    occurrences: list[str]
    verification_request: ProblemVerificationRequest | None
    resolution: ProblemResolution | None
    version: int
    merged_into: str | None = None


def _assert_version(state: _ProblemState, expected: int) -> None:
    if state.version != expected:
        raise ProjectionError(
            f"Problem {state.problem_id}: expected version {expected}, found {state.version}"
        )


def _canonical_id(value: str) -> str:
    """16-hex-char prefix of SHA-256 for deterministic entry IDs."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# project_change_issues
# ---------------------------------------------------------------------------


def project_change_issues(events: Sequence[ChangeIssueEvent]) -> ChangeIssueSnapshot:
    """Fold Change Issue events into a ChangeIssueSnapshot.

    Observations and Occurrences from all batches are retained.  The snapshot
    tracks the latest authoritative batch (last completed/failed analysis) and
    the current project_sync_status.
    """
    from assurance_agent.workflow.issues.events import (
        IssueAnalysisCompletedEvent,
        IssueAnalysisFailedEvent,
        ObservationRecordedEvent,
        OccurrenceDetectedEvent,
        OccurrenceLinkedEvent,
        ProjectSyncPendingEvent,
    )

    change_id: str | None = None
    observations: list[Observation] = []
    occurrences: list[IssueOccurrence] = []
    analysis_status: IssueAnalysisStatus | None = None
    project_sync_status: ProjectSyncStatus = "completed"
    batches_seen: list[str] = []

    def _record_batch(batch_id: str) -> None:
        if batch_id not in batches_seen:
            batches_seen.append(batch_id)

    authoritative_batch_id: str | None = None

    for event in events:
        if change_id is None:
            change_id = event.change_id

        batch_id: str = event.batch_id
        _record_batch(batch_id)

        if isinstance(event, ObservationRecordedEvent):
            observations.append(event.observation)

        elif isinstance(event, IssueAnalysisCompletedEvent):
            analysis_status = event.analysis_status
            authoritative_batch_id = batch_id
            project_sync_status = "completed"

        elif isinstance(event, IssueAnalysisFailedEvent):
            analysis_status = event.analysis_status
            authoritative_batch_id = batch_id

        elif isinstance(event, (OccurrenceDetectedEvent, OccurrenceLinkedEvent)):
            occurrences.append(event.occurrence)
            project_sync_status = "completed"

        elif isinstance(event, ProjectSyncPendingEvent):
            project_sync_status = "pending"

    if change_id is None or not batches_seen:
        raise ProjectionError("no events to project: cannot build ChangeIssueSnapshot")

    if authoritative_batch_id is None:
        authoritative_batch_id = batches_seen[-1]

    return ChangeIssueSnapshot(
        schema_version="1.0",
        change_id=change_id,
        authoritative_batch_id=authoritative_batch_id,
        observations=observations,
        occurrences=occurrences,
        analysis_status=analysis_status,
        project_sync_status=project_sync_status,
        batches=batches_seen,
    )


# ---------------------------------------------------------------------------
# project_problems
# ---------------------------------------------------------------------------


def project_problems(events: Sequence[ProblemEvent]) -> ProblemProjection:
    """Fold Project Problem events into a ProblemProjection.

    Version checking is strict: an event whose expected_problem_version does
    not match the current version raises ProjectionError.  This should never
    happen for events that passed ledger append validation; it is a safety net.

    problem_merged leaves source history intact (status → resolved with
    disposition "merged_into:<target_id>") and records a canonical alias so
    the canonical identity of the source resolves to the target.
    """
    from assurance_agent.workflow.issues.events import (
        ProblemAssessmentConfirmedEvent,
        ProblemDetectedEvent,
        ProblemMergedEvent,
        ProblemMergeSuggestedEvent,
        ProblemOccurrenceLinkedEvent,
        ProblemRegressedEvent,
        ProblemResolvedEvent,
        ProblemReopenedEvent,
        ProblemVerificationRequestedEvent,
        ProblemWorkStartedEvent,
        ProblemMarkedNotAnIssueEvent,
        ProblemRiskAcceptedEvent,
    )

    states: dict[str, _ProblemState] = {}
    last_ts: str = ""

    for event in events:
        last_ts = event.ts

        if isinstance(event, ProblemDetectedEvent):
            if event.expected_problem_version != 0:
                raise ProjectionError(
                    f"problem_detected for {event.problem_id} must have "
                    f"expected_problem_version=0, got {event.expected_problem_version}"
                )
            if event.problem_id in states:
                raise ProjectionError(f"problem_detected: {event.problem_id} already exists")
            first_ref = ProblemSeenRef(
                change_id=event.change_id,
                occurrence_id=event.occurrence_id,
            )
            states[event.problem_id] = _ProblemState(
                problem_id=event.problem_id,
                fingerprint=event.fingerprint,
                title=event.title,
                classification=event.classification,
                severity=event.severity,
                authority="llm_provisional",
                root_cause_hypothesis=event.root_cause_hypothesis,
                status="detected",
                first_seen=first_ref,
                last_seen=first_ref,
                occurrences=[event.occurrence_id],
                verification_request=None,
                resolution=None,
                version=1,
            )

        elif isinstance(event, ProblemOccurrenceLinkedEvent):
            pid = event.problem_id
            if pid not in states:
                raise ProjectionError(f"problem_occurrence_linked: unknown {pid}")
            state = states[pid]
            _assert_version(state, event.expected_problem_version)
            state.occurrences = [*state.occurrences, event.occurrence_id]
            state.last_seen = ProblemSeenRef(
                change_id=event.change_id,
                occurrence_id=event.occurrence_id,
            )
            state.version += 1

        elif isinstance(event, ProblemAssessmentConfirmedEvent):
            pid = event.problem_id
            if pid not in states:
                raise ProjectionError(f"problem_assessment_confirmed: unknown {pid}")
            state = states[pid]
            _assert_version(state, event.expected_problem_version)
            state.classification = event.classification
            state.severity = event.severity
            state.authority = "human_confirmed"
            state.root_cause_hypothesis = event.root_cause_hypothesis
            state.status = "triaged"
            state.version += 1

        elif isinstance(event, ProblemWorkStartedEvent):
            pid = event.problem_id
            if pid not in states:
                raise ProjectionError(f"problem_work_started: unknown {pid}")
            state = states[pid]
            _assert_version(state, event.expected_problem_version)
            state.status = "in_progress"
            state.version += 1

        elif isinstance(event, ProblemVerificationRequestedEvent):
            pid = event.problem_id
            if pid not in states:
                raise ProjectionError(f"problem_verification_requested: unknown {pid}")
            state = states[pid]
            _assert_version(state, event.expected_problem_version)
            state.status = "verification_pending"
            state.verification_request = ProblemVerificationRequest(
                requested_at=event.ts,
                change_id=event.change_id,
                batch_id=event.batch_id,
                linked_fix_disposition=event.linked_fix_disposition,
                verification_scope=event.verification_scope,
                evidence_digest=event.evidence_digest,
            )
            state.version += 1

        elif isinstance(event, ProblemResolvedEvent):
            pid = event.problem_id
            if pid not in states:
                raise ProjectionError(f"problem_resolved: unknown {pid}")
            state = states[pid]
            _assert_version(state, event.expected_problem_version)
            state.resolution = ProblemResolution(
                resolved_at=event.resolved_at,
                change_id=event.change_id,
                batch_id=event.batch_id,
                disposition=event.disposition,
                verification_scope=event.verification_scope,
                evidence_digest=event.evidence_digest,
            )
            state.status = "resolved"
            state.verification_request = None
            state.version += 1

        elif isinstance(event, ProblemMarkedNotAnIssueEvent):
            pid = event.problem_id
            if pid not in states:
                raise ProjectionError(f"problem_marked_not_an_issue: unknown {pid}")
            state = states[pid]
            _assert_version(state, event.expected_problem_version)
            state.status = "not_an_issue"
            state.verification_request = None
            state.version += 1

        elif isinstance(event, ProblemRiskAcceptedEvent):
            pid = event.problem_id
            if pid not in states:
                raise ProjectionError(f"problem_risk_accepted: unknown {pid}")
            state = states[pid]
            _assert_version(state, event.expected_problem_version)
            state.status = "accepted_risk"
            state.verification_request = None
            state.version += 1

        elif isinstance(event, ProblemReopenedEvent):
            pid = event.problem_id
            if pid not in states:
                raise ProjectionError(f"problem_reopened: unknown {pid}")
            state = states[pid]
            _assert_version(state, event.expected_problem_version)
            state.status = "detected"
            state.verification_request = None
            state.version += 1

        elif isinstance(event, ProblemRegressedEvent):
            pid = event.problem_id
            if pid not in states:
                raise ProjectionError(f"problem_regressed: unknown {pid}")
            state = states[pid]
            _assert_version(state, event.expected_problem_version)
            state.occurrences = [*state.occurrences, event.occurrence_id]
            state.last_seen = ProblemSeenRef(
                change_id=event.change_id,
                occurrence_id=event.occurrence_id,
            )
            state.status = "detected"
            state.verification_request = None
            state.resolution = None
            state.version += 1

        elif isinstance(event, ProblemMergeSuggestedEvent):
            pass  # handled only in project_review_queue

        elif isinstance(event, ProblemMergedEvent):
            pid = event.problem_id
            if pid not in states:
                raise ProjectionError(f"problem_merged: unknown {pid}")
            state = states[pid]
            _assert_version(state, event.expected_problem_version)
            state.merged_into = event.target_problem_id
            state.resolution = ProblemResolution(
                resolved_at=event.resolved_at,
                change_id="merged",
                batch_id="merged",
                disposition=f"merged_into:{event.target_problem_id}",
                verification_scope=["merged"],
                evidence_digest=event.evidence_digest,
            )
            state.status = "resolved"
            state.verification_request = None
            state.version += 1

    problems: list[Problem] = []
    for state in states.values():
        assessment = ProblemAssessment(
            classification=state.classification,
            severity=state.severity,
            authority=state.authority,
            root_cause_hypothesis=state.root_cause_hypothesis,
        )
        problems.append(
            Problem(
                problem_id=state.problem_id,
                fingerprint=state.fingerprint,
                title=state.title,
                assessment=assessment,
                status=state.status,
                first_seen=state.first_seen,
                last_seen=state.last_seen,
                occurrences=state.occurrences,
                verification_request=state.verification_request,
                resolution=state.resolution,
                version=state.version,
            )
        )

    return ProblemProjection(
        schema_version="1.0",
        generated_at=last_ts or "1970-01-01T00:00:00Z",
        problems=problems,
    )


# ---------------------------------------------------------------------------
# project_review_queue
# ---------------------------------------------------------------------------


def project_review_queue(events: Sequence[ProblemEvent]) -> ProblemReviewQueue:
    """Derive the ProblemReviewQueue from merge-suggestion events.

    Each ``problem_merge_suggested`` event creates one queue entry.  Entries
    are deterministic: the entry_id is derived from the event_id so the same
    event bytes always produce the same entry.
    """
    from assurance_agent.workflow.issues.events import ProblemMergeSuggestedEvent

    entries: list[ProblemReviewQueueEntry] = []
    for event in events:
        if isinstance(event, ProblemMergeSuggestedEvent):
            entry_id = f"QE-{_canonical_id(event.event_id)}"
            entries.append(
                ProblemReviewQueueEntry(
                    entry_id=entry_id,
                    change_id=event.source_change_id,
                    occurrence_id=event.source_occurrence_id,
                    candidate_id=event.candidate_id,
                    possible_problem_ids=[event.target_problem_id],
                    reason=event.reason,
                    created_at=event.ts,
                )
            )

    return ProblemReviewQueue(schema_version="1.0", entries=entries)


# ---------------------------------------------------------------------------
# dump_projection
# ---------------------------------------------------------------------------


def dump_projection(model: BaseModel) -> bytes:
    """Serialize a projection model as canonical JSON bytes.

    Uses compact separators and sorted keys so that identical models always
    produce byte-identical output regardless of dict insertion order.  A
    single trailing newline is included for POSIX compliance.
    """
    data = model.model_dump(mode="json")
    return (json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode(
        "utf-8"
    )
