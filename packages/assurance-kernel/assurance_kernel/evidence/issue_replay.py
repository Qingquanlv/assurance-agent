"""Strict issue ledger replay and pure projections below workflow.

Authority loaders raise on missing paths. Projection helpers are filesystem-free.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel, TypeAdapter, ValidationError

from assurance_kernel.artifacts.models.issue_events import (
    CHANGE_ISSUE_EVENT_ADAPTER,
    PROBLEM_EVENT_ADAPTER,
    ChangeIssueEvent,
    IssueAnalysisCompletedEvent,
    IssueAnalysisFailedEvent,
    ObservationRecordedEvent,
    OccurrenceDetectedEvent,
    OccurrenceLinkedEvent,
    ProblemAssessmentConfirmedEvent,
    ProblemDetectedEvent,
    ProblemEvent,
    ProblemMarkedNotAnIssueEvent,
    ProblemMergedEvent,
    ProblemMergeSuggestedEvent,
    ProblemOccurrenceLinkedEvent,
    ProblemRegressedEvent,
    ProblemReopenedEvent,
    ProblemResolvedEvent,
    ProblemRiskAcceptedEvent,
    ProblemVerificationRequestedEvent,
    ProblemWorkStartedEvent,
    ProjectSyncPendingEvent,
)
from assurance_kernel.artifacts.models.issues import (
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
from assurance_kernel.evidence.issue_identity import (
    ObservationIdentityInput,
    event_id,
    observation_id,
    occurrence_id,
    problem_id,
    recomputable_issue_event_idempotency_key,
)
from assurance_kernel.exceptions import AaError

_EventT = TypeVar("_EventT")


class IssueLedgerMissingError(AaError):
    """Raised when an authority ledger path is absent."""


class IssueLedgerIntegrityError(AaError):
    """Raised when a ledger fails strict identity or structural validation."""


class ProjectionError(Exception):
    """Raised when an event cannot be applied to the current projected state."""


def _evidence_refs_digest(evidence_refs: Sequence[str]) -> str:
    canonical = json.dumps(sorted(evidence_refs), sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_event_nested_identity(event: ChangeIssueEvent | ProblemEvent) -> None:
    """Validate envelope ↔ nested payload identity relations for one event."""
    if isinstance(event, ObservationRecordedEvent):
        obs = event.observation
        if obs.change_id != event.change_id:
            raise IssueLedgerIntegrityError("observation change_id mismatch")
        if obs.batch_id != event.batch_id:
            raise IssueLedgerIntegrityError("observation batch_id mismatch")
        expected = observation_id(
            ObservationIdentityInput(
                change_id=obs.change_id,
                batch_id=obs.batch_id,
                kind=obs.kind,
                target=obs.target,
                case_id=obs.case_id,
                source_artifact=obs.source.artifact,
                source_json_pointer=obs.source.json_pointer,
                signature=obs.signature,
            )
        )
        if obs.observation_id != expected:
            raise IssueLedgerIntegrityError("observation_id mismatch")
        return

    if isinstance(event, (IssueAnalysisCompletedEvent, IssueAnalysisFailedEvent)):
        status = event.analysis_status
        if status.change_id != event.change_id:
            raise IssueLedgerIntegrityError("analysis_status change_id mismatch")
        if status.batch_id != event.batch_id:
            raise IssueLedgerIntegrityError("analysis_status batch_id mismatch")
        if status.evidence_bundle_digest != event.evidence_digest:
            raise IssueLedgerIntegrityError("evidence_digest mismatch")
        return

    if isinstance(event, (OccurrenceDetectedEvent, OccurrenceLinkedEvent)):
        occ = event.occurrence
        if occ.change_id != event.change_id:
            raise IssueLedgerIntegrityError("occurrence change_id mismatch")
        if occ.batch_id != event.batch_id:
            raise IssueLedgerIntegrityError("occurrence batch_id mismatch")
        if occ.analysis.evidence_bundle_digest != event.evidence_digest:
            raise IssueLedgerIntegrityError("evidence_digest mismatch")
        expected_occ = occurrence_id(
            occ.change_id,
            occ.batch_id,
            occ.analysis.candidate_digest,
        )
        if occ.occurrence_id != expected_occ:
            raise IssueLedgerIntegrityError("occurrence_id mismatch")
        return

    if isinstance(event, ProjectSyncPendingEvent):
        return

    if isinstance(event, ProblemDetectedEvent):
        if event.expected_problem_version != 0:
            raise IssueLedgerIntegrityError("expected_problem_version mismatch")
        if event.problem_id != problem_id(event.fingerprint):
            raise IssueLedgerIntegrityError("problem_id mismatch")
        return

    if isinstance(
        event,
        (
            ProblemAssessmentConfirmedEvent,
            ProblemWorkStartedEvent,
            ProblemMarkedNotAnIssueEvent,
            ProblemRiskAcceptedEvent,
            ProblemReopenedEvent,
            ProblemMergedEvent,
        ),
    ):
        if event.evidence_digest != _evidence_refs_digest(event.evidence_refs):
            raise IssueLedgerIntegrityError("evidence_digest mismatch")
        return


def validate_event_identity(event: ChangeIssueEvent | ProblemEvent) -> None:
    if event.event_id != event_id(event.idempotency_key):
        raise IssueLedgerIntegrityError("event_id mismatch")
    expected_key = recomputable_issue_event_idempotency_key(event)
    if expected_key is not None and event.idempotency_key != expected_key:
        raise IssueLedgerIntegrityError("idempotency_key mismatch")
    validate_event_nested_identity(event)


def _defining_observation_id(event: ChangeIssueEvent | ProblemEvent) -> str | None:
    if isinstance(event, ObservationRecordedEvent):
        return event.observation.observation_id
    return None


def _defining_occurrence_id(event: ChangeIssueEvent | ProblemEvent) -> str | None:
    if isinstance(event, (OccurrenceDetectedEvent, OccurrenceLinkedEvent)):
        return event.occurrence.occurrence_id
    return None


def _read_strict_bytes(
    data: bytes,
    adapter: TypeAdapter[_EventT],
    source: str,
) -> tuple[_EventT, ...]:
    if not data:
        return ()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise IssueLedgerIntegrityError(f"{source}: invalid UTF-8: {exc}") from exc

    events: list[_EventT] = []
    seen_event_ids: set[str] = set()
    seen_idempotency_keys: set[str] = set()
    seen_observation_ids: set[str] = set()
    seen_occurrence_ids: set[str] = set()
    expected_seq = 1

    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        if not raw_line.strip():
            raise IssueLedgerIntegrityError(f"{source} line {line_no}: blank hole in ledger")

        try:
            payload = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise IssueLedgerIntegrityError(f"{source} line {line_no}: invalid JSON: {exc}") from exc

        if not isinstance(payload, dict):
            raise IssueLedgerIntegrityError(f"{source} line {line_no}: event is not a JSON object")

        seq = payload.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool) or seq != expected_seq:
            raise IssueLedgerIntegrityError(
                f"{source} line {line_no}: expected seq {expected_seq}, got {seq!r}"
            )

        try:
            event = adapter.validate_python(payload)
        except ValidationError as exc:
            raise IssueLedgerIntegrityError(f"{source} line {line_no}: invalid event: {exc}") from exc

        event_id_value: str = event.event_id  # type: ignore[attr-defined]
        idempotency_key: str = event.idempotency_key  # type: ignore[attr-defined]

        if event_id_value in seen_event_ids:
            raise IssueLedgerIntegrityError(f"{source} line {line_no}: duplicate event_id {event_id_value!r}")
        if idempotency_key in seen_idempotency_keys:
            raise IssueLedgerIntegrityError(
                f"{source} line {line_no}: duplicate idempotency_key {idempotency_key!r}"
            )

        try:
            validate_event_identity(event)  # type: ignore[arg-type]
        except IssueLedgerIntegrityError as exc:
            raise IssueLedgerIntegrityError(f"{source} line {line_no}: {exc}") from exc

        defined_obs = _defining_observation_id(event)  # type: ignore[arg-type]
        if defined_obs is not None:
            if defined_obs in seen_observation_ids:
                raise IssueLedgerIntegrityError(
                    f"{source} line {line_no}: duplicate defining observation_id {defined_obs!r}"
                )
            seen_observation_ids.add(defined_obs)

        defined_occ = _defining_occurrence_id(event)  # type: ignore[arg-type]
        if defined_occ is not None:
            if defined_occ in seen_occurrence_ids:
                raise IssueLedgerIntegrityError(
                    f"{source} line {line_no}: duplicate defining occurrence_id {defined_occ!r}"
                )
            seen_occurrence_ids.add(defined_occ)

        seen_event_ids.add(event_id_value)
        seen_idempotency_keys.add(idempotency_key)
        events.append(event)
        expected_seq += 1

    return tuple(events)


def read_change_issue_events_from_bytes(data: bytes) -> tuple[ChangeIssueEvent, ...]:
    return _read_strict_bytes(data, CHANGE_ISSUE_EVENT_ADAPTER, "<bytes>")


def read_problem_events_from_bytes(data: bytes) -> tuple[ProblemEvent, ...]:
    return _read_strict_bytes(data, PROBLEM_EVENT_ADAPTER, "<bytes>")


def load_change_issue_ledger(path: Path) -> tuple[ChangeIssueEvent, ...]:
    if not path.is_file():
        raise IssueLedgerMissingError(str(path))
    return _read_strict_bytes(path.read_bytes(), CHANGE_ISSUE_EVENT_ADAPTER, str(path))


def load_problem_ledger(path: Path) -> tuple[ProblemEvent, ...]:
    if not path.is_file():
        raise IssueLedgerMissingError(str(path))
    return _read_strict_bytes(path.read_bytes(), PROBLEM_EVENT_ADAPTER, str(path))


# ---------------------------------------------------------------------------
# Pure projections (moved unchanged from workflow.issues.projection)
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


def project_change_issues(events: Sequence[ChangeIssueEvent]) -> ChangeIssueSnapshot:
    """Fold Change Issue events into a ChangeIssueSnapshot."""
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


def project_problems(events: Sequence[ProblemEvent]) -> ProblemProjection:
    """Fold Project Problem events into a ProblemProjection."""
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


def project_review_queue(events: Sequence[ProblemEvent]) -> ProblemReviewQueue:
    """Derive one active human-review request per Problem."""
    projection = project_problems(events)
    active = {problem.problem_id: problem for problem in projection.problems}
    detected: dict[str, ProblemDetectedEvent] = {}
    suggestions: dict[str, list[ProblemMergeSuggestedEvent]] = {}
    for event in events:
        if isinstance(event, ProblemDetectedEvent):
            detected[event.problem_id] = event
        elif isinstance(event, ProblemMergeSuggestedEvent):
            suggestions.setdefault(event.problem_id, []).append(event)

    entries: list[ProblemReviewQueueEntry] = []
    for problem_id_value in sorted(active):
        problem = active[problem_id_value]
        detection = detected.get(problem_id_value)
        if detection is None or problem.status == "resolved":
            continue
        possible = sorted({event.target_problem_id for event in suggestions.get(problem_id_value, [])})
        provisional = problem.status == "detected" and problem.assessment.authority == "llm_provisional"
        if not provisional and not possible:
            continue
        merge_events = suggestions.get(problem_id_value, [])
        candidate_id = next(
            (event.candidate_id for event in reversed(merge_events) if event.candidate_id is not None),
            None,
        )
        if provisional and possible:
            reason = "provisional assessment and possible matches require human review"
        elif provisional:
            reason = "provisional assessment requires human review"
        else:
            reason = "possible matches require human review"
        entries.append(
            ProblemReviewQueueEntry(
                entry_id=f"QE-{_canonical_id(problem_id_value)}",
                problem_id=problem_id_value,
                change_id=detection.change_id,
                occurrence_id=detection.occurrence_id,
                candidate_id=candidate_id,
                possible_problem_ids=possible,
                reason=reason,
                created_at=detection.ts,
            )
        )

    return ProblemReviewQueue(schema_version="1.0", entries=entries)


def dump_projection(model: BaseModel) -> bytes:
    """Serialize a projection model as canonical JSON bytes."""
    data = model.model_dump(mode="json")
    return (json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode(
        "utf-8"
    )
