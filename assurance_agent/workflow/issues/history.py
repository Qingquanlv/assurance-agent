"""Read-only IssueHistoryReader seam for Retro evidence collection.

Production adapter reads archive-first Change Issue ledgers and a
retry-stabilized Project Problem ledger. Projections are never authoritative:
when a projection file exists it is replayed from events and rejected on
mismatch. This module has no ledger writer path.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Protocol, runtime_checkable

from pydantic import ValidationError

from assurance_agent.artifacts.models.issues import IssueOccurrence, Observation, Problem
from assurance_agent.change_location import ChangeNotFoundError, resolve_change
from assurance_agent.exceptions import AaError
from assurance_agent.workflow.issues.events import (
    CHANGE_ISSUE_EVENT_ADAPTER,
    PROBLEM_EVENT_ADAPTER,
    ChangeIssueEvent,
    IssueAnalysisFailedEvent,
    LedgerIntegrityError,
    ObservationRecordedEvent,
    OccurrenceDetectedEvent,
    OccurrenceLinkedEvent,
    ProblemEvent,
    ProblemMergedEvent,
    ProjectSyncPendingEvent,
    read_change_issue_events,
)
from assurance_agent.workflow.issues.history_models import (
    IssueEvidenceSlice,
    IssueHistoryIntegrity,
    IssueIntegrityReason,
    IssueSourceDescriptor,
    IssueTypedEvents,
    IssueWindowSelection,
)
from assurance_agent.workflow.issues.projection import (
    dump_projection,
    project_change_issues,
    project_problems,
)

_REVIEWISH_PROBLEM_TYPES = frozenset(
    {
        "problem_assessment_confirmed",
        "problem_work_started",
        "problem_verification_requested",
        "problem_resolved",
        "problem_marked_not_an_issue",
        "problem_risk_accepted",
        "problem_reopened",
        "problem_regressed",
        "problem_merge_suggested",
        "problem_merged",
    }
)


class IssueHistoryConflict(AaError):
    """Raised when the Project Problem ledger head changes during a stable read."""


class IssueHistoryIntegrityError(AaError):
    """Raised when an Issue ledger is corrupt or a projection mismatches events."""


def read_problem_events_from_bytes(data: bytes) -> list[ProblemEvent]:
    """Strictly validate Project Problem events from in-memory ledger bytes."""
    return _read_events_from_bytes(data, PROBLEM_EVENT_ADAPTER)  # type: ignore[return-value]


def read_change_issue_events_from_bytes(data: bytes) -> list[ChangeIssueEvent]:
    """Strictly validate Change Issue events from in-memory ledger bytes."""
    return _read_events_from_bytes(data, CHANGE_ISSUE_EVENT_ADAPTER)  # type: ignore[return-value]


def _read_events_from_bytes(data: bytes, adapter: object) -> list[object]:
    if not data:
        return []
    text = data.decode("utf-8")
    events: list[object] = []
    seen_event_ids: set[str] = set()
    seen_idempotency_keys: set[str] = set()
    expected_seq = 1
    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        if not raw_line.strip():
            raise LedgerIntegrityError(f"<bytes> line {line_no}: blank hole in ledger")
        try:
            payload = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise LedgerIntegrityError(f"<bytes> line {line_no}: invalid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise LedgerIntegrityError(f"<bytes> line {line_no}: event is not a JSON object")
        seq = payload.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool) or seq != expected_seq:
            raise LedgerIntegrityError(f"<bytes> line {line_no}: expected seq {expected_seq}, got {seq!r}")
        try:
            event = adapter.validate_python(payload)  # type: ignore[attr-defined]
        except ValidationError as exc:
            raise LedgerIntegrityError(f"<bytes> line {line_no}: invalid event: {exc}") from exc
        event_id: str = event.event_id
        idempotency_key: str = event.idempotency_key
        if event_id in seen_event_ids:
            raise LedgerIntegrityError(f"<bytes> line {line_no}: duplicate event_id {event_id!r}")
        if idempotency_key in seen_idempotency_keys:
            raise LedgerIntegrityError(
                f"<bytes> line {line_no}: duplicate idempotency_key {idempotency_key!r}"
            )
        seen_event_ids.add(event_id)
        seen_idempotency_keys.add(idempotency_key)
        events.append(event)
        expected_seq += 1
    return events


def _sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _pin_problem_events(
    events: Sequence[ProblemEvent],
    project_event_through: str | None,
) -> list[ProblemEvent]:
    if project_event_through is None:
        return list(events)
    pinned: list[ProblemEvent] = []
    for event in events:
        pinned.append(event)
        if event.event_id == project_event_through:
            return pinned
    raise IssueHistoryIntegrityError(
        f"project_event_through {project_event_through!r} not found in Problem ledger"
    )


def _ts_in_range(ts: str, since: str | None, until: str | None) -> bool:
    if since is not None and ts < since:
        return False
    if until is not None and ts > until:
        return False
    return True


def _collect_problem_ids_from_change_events(events: Sequence[ChangeIssueEvent]) -> set[str]:
    problem_ids: set[str] = set()
    for event in events:
        if isinstance(event, (OccurrenceDetectedEvent, OccurrenceLinkedEvent)):
            problem_ids.add(event.occurrence.problem_id)
    return problem_ids


def _expand_merge_aliases(
    problem_ids: set[str],
    problem_events: Sequence[ProblemEvent],
) -> set[str]:
    expanded = set(problem_ids)
    changed = True
    while changed:
        changed = False
        for event in problem_events:
            if not isinstance(event, ProblemMergedEvent):
                continue
            if event.problem_id in expanded and event.target_problem_id not in expanded:
                expanded.add(event.target_problem_id)
                changed = True
            if event.target_problem_id in expanded and event.problem_id not in expanded:
                expanded.add(event.problem_id)
                changed = True
    return expanded


def _problem_event_change_id(event: ProblemEvent) -> str | None:
    return getattr(event, "change_id", None) or getattr(event, "source_change_id", None)


def _filter_problem_events_for_ids(
    problem_events: Sequence[ProblemEvent],
    problem_ids: set[str],
) -> list[ProblemEvent]:
    return [event for event in problem_events if event.problem_id in problem_ids]


def _observations_from_events(events: Sequence[ChangeIssueEvent]) -> list[Observation]:
    return [event.observation for event in events if isinstance(event, ObservationRecordedEvent)]


def _occurrences_from_events(events: Sequence[ChangeIssueEvent]) -> list[IssueOccurrence]:
    return [
        event.occurrence
        for event in events
        if isinstance(event, (OccurrenceDetectedEvent, OccurrenceLinkedEvent))
    ]


def _integrity_for_change_events(
    change_events_by_id: Mapping[str, Sequence[ChangeIssueEvent]],
) -> IssueHistoryIntegrity:
    reasons: list[IssueIntegrityReason] = []
    for change_id in sorted(change_events_by_id):
        events = change_events_by_id[change_id]
        if any(isinstance(event, IssueAnalysisFailedEvent) for event in events):
            # Prefer latest analysis outcome from projection when available.
            try:
                snapshot = project_change_issues(events)
            except Exception:
                snapshot = None
            if snapshot is None or (
                snapshot.analysis_status is not None and snapshot.analysis_status.status == "failed"
            ):
                if "analysis_failed" not in reasons:
                    reasons.append("analysis_failed")
        if any(isinstance(event, ProjectSyncPendingEvent) for event in events):
            try:
                snapshot = project_change_issues(events)
            except Exception:
                snapshot = None
            if snapshot is None or snapshot.project_sync_status == "pending":
                if "project_sync_pending" not in reasons:
                    reasons.append("project_sync_pending")
    if reasons:
        return IssueHistoryIntegrity(status="incomplete", reasons=tuple(reasons))
    return IssueHistoryIntegrity(status="complete")


def _sort_observations(items: Sequence[Observation]) -> tuple[Observation, ...]:
    return tuple(sorted(items, key=lambda item: item.observation_id))


def _sort_occurrences(items: Sequence[IssueOccurrence]) -> tuple[IssueOccurrence, ...]:
    return tuple(sorted(items, key=lambda item: item.occurrence_id))


def _sort_problems(items: Sequence[Problem]) -> tuple[Problem, ...]:
    return tuple(sorted(items, key=lambda item: item.problem_id))


def build_issue_evidence_slice(
    selection: IssueWindowSelection,
    *,
    change_events_by_id: Mapping[str, Sequence[ChangeIssueEvent]],
    problem_events: Sequence[ProblemEvent],
    change_ledger_bytes: Mapping[str, bytes],
    problem_ledger_bytes: bytes,
    all_change_events_by_id: Mapping[str, Sequence[ChangeIssueEvent]] | None = None,
) -> IssueEvidenceSlice:
    """Pure reference-closure builder shared by file and memory adapters."""
    catalog = all_change_events_by_id if all_change_events_by_id is not None else change_events_by_id
    pinned_problems = _pin_problem_events(problem_events, selection.project_event_through)

    time_bounded = selection.event_since is not None or selection.event_until is not None
    if selection.change_ids:
        selected_change_ids = tuple(sorted(selection.change_ids))
        selected_change_events = {
            change_id: list(change_events_by_id.get(change_id, ())) for change_id in selected_change_ids
        }
    elif time_bounded:
        selected_change_events = {}
        for change_id, events in catalog.items():
            in_range = [
                event
                for event in events
                if _ts_in_range(event.ts, selection.event_since, selection.event_until)
            ]
            if in_range:
                # Keep full change records needed to explain in-range events.
                selected_change_events[change_id] = list(events)
        selected_change_ids = tuple(sorted(selected_change_events))
    else:
        selected_change_ids = tuple(sorted(change_events_by_id))
        selected_change_events = {
            change_id: list(change_events_by_id[change_id]) for change_id in selected_change_ids
        }

    problem_ids = set()
    for events in selected_change_events.values():
        problem_ids |= _collect_problem_ids_from_change_events(events)

    if time_bounded:
        for event in pinned_problems:
            if _ts_in_range(event.ts, selection.event_since, selection.event_until):
                problem_ids.add(event.problem_id)
                change_id = _problem_event_change_id(event)
                if change_id and change_id in catalog and change_id not in selected_change_events:
                    selected_change_events[change_id] = list(catalog[change_id])

    problem_ids = _expand_merge_aliases(problem_ids, pinned_problems)
    relevant_problem_events = _filter_problem_events_for_ids(pinned_problems, problem_ids)

    observations = [
        obs for events in selected_change_events.values() for obs in _observations_from_events(events)
    ]
    occurrences = [
        occ for events in selected_change_events.values() for occ in _occurrences_from_events(events)
    ]

    needed_occurrence_ids: set[str] = set()
    needed_change_ids: set[str] = set(selected_change_events)
    for event in relevant_problem_events:
        occurrence_id = getattr(event, "occurrence_id", None)
        if isinstance(occurrence_id, str):
            needed_occurrence_ids.add(occurrence_id)
        source_occurrence_id = getattr(event, "source_occurrence_id", None)
        if isinstance(source_occurrence_id, str):
            needed_occurrence_ids.add(source_occurrence_id)
        change_id = _problem_event_change_id(event)
        if isinstance(change_id, str):
            if (
                selection.include_late_review_closure
                or time_bounded
                or event.type in _REVIEWISH_PROBLEM_TYPES
            ):
                if selection.include_late_review_closure or time_bounded:
                    needed_change_ids.add(change_id)

    if selection.include_late_review_closure or time_bounded:
        for change_id in sorted(needed_change_ids):
            if change_id in selected_change_events:
                continue
            if change_id not in catalog:
                continue
            selected_change_events[change_id] = list(catalog[change_id])
            observations.extend(_observations_from_events(selected_change_events[change_id]))
            occurrences.extend(_occurrences_from_events(selected_change_events[change_id]))

        # Pull any still-missing occurrences referenced by problem events.
        known_occ = {item.occurrence_id for item in occurrences}
        for change_id, events in catalog.items():
            for occurrence in _occurrences_from_events(events):
                if (
                    occurrence.occurrence_id in needed_occurrence_ids
                    and occurrence.occurrence_id not in known_occ
                ):
                    occurrences.append(occurrence)
                    known_occ.add(occurrence.occurrence_id)
                    if change_id not in selected_change_events:
                        selected_change_events[change_id] = list(events)
                    for observation in _observations_from_events(events):
                        if observation.observation_id in occurrence.observation_ids:
                            observations.append(observation)

    selected_change_ids = tuple(sorted(selected_change_events))
    projection = project_problems(relevant_problem_events) if relevant_problem_events else None
    problem_snapshots = _sort_problems(projection.problems if projection is not None else ())

    sources: list[IssueSourceDescriptor] = []
    for change_id in selected_change_ids:
        events = selected_change_events[change_id]
        raw = change_ledger_bytes.get(change_id, b"")
        head = events[-1].event_id if events else None
        sources.append(
            IssueSourceDescriptor(
                kind="change_issue_ledger",
                change_id=change_id,
                head_event_id=head,
                sha256=_sha256_bytes(raw),
            )
        )
    problem_head = (
        relevant_problem_events[-1].event_id
        if relevant_problem_events
        else (pinned_problems[-1].event_id if pinned_problems else None)
    )
    # Source descriptor pins the full ledger bytes actually read (through stable head),
    # while problem_events on the slice are the relevant closed subset.
    sources.append(
        IssueSourceDescriptor(
            kind="project_problem_ledger",
            change_id=None,
            head_event_id=(
                selection.project_event_through
                if selection.project_event_through is not None
                else problem_head
            ),
            sha256=_sha256_bytes(problem_ledger_bytes),
        )
    )

    integrity = _integrity_for_change_events(selected_change_events)
    return IssueEvidenceSlice(
        selection=selection,
        sources=tuple(sources),
        integrity=integrity,
        observations=_sort_observations(observations),
        occurrences=_sort_occurrences(occurrences),
        problem_snapshots=problem_snapshots,
        problem_events=tuple(relevant_problem_events),
    )


@runtime_checkable
class IssueHistoryReader(Protocol):
    def read_window(self, selection: IssueWindowSelection) -> IssueEvidenceSlice: ...


class InMemoryIssueHistoryReader:
    """Test adapter over typed Change/Project events (no filesystem writes)."""

    def __init__(
        self,
        *,
        change_events: Mapping[str, Sequence[ChangeIssueEvent]],
        problem_events: Sequence[ProblemEvent],
        change_ledger_bytes: Mapping[str, bytes],
        problem_ledger_bytes: bytes,
    ) -> None:
        self._change_events = {key: tuple(value) for key, value in change_events.items()}
        self._problem_events = tuple(problem_events)
        self._change_ledger_bytes = dict(change_ledger_bytes)
        self._problem_ledger_bytes = problem_ledger_bytes

    @classmethod
    def from_events(cls, typed_events: IssueTypedEvents) -> InMemoryIssueHistoryReader:
        return cls(
            change_events=typed_events.change_events,
            problem_events=typed_events.problem_events,
            change_ledger_bytes=typed_events.change_ledger_bytes,
            problem_ledger_bytes=typed_events.problem_ledger_bytes,
        )

    def read_window(self, selection: IssueWindowSelection) -> IssueEvidenceSlice:
        try:
            for raw in self._change_ledger_bytes.values():
                read_change_issue_events_from_bytes(raw)
            read_problem_events_from_bytes(self._problem_ledger_bytes)
        except LedgerIntegrityError as exc:
            raise IssueHistoryIntegrityError(str(exc)) from exc
        for change_id in selection.change_ids:
            if change_id not in self._change_events and change_id not in self._change_ledger_bytes:
                raise IssueHistoryIntegrityError(
                    f"change '{change_id}' not found in in-memory issue history bundle"
                )
        return build_issue_evidence_slice(
            selection,
            change_events_by_id=self._change_events,
            problem_events=self._problem_events,
            change_ledger_bytes=self._change_ledger_bytes,
            problem_ledger_bytes=self._problem_ledger_bytes,
            all_change_events_by_id=self._change_events,
        )


class LedgerIssueHistoryReader:
    """Production adapter: archive-first Change ledgers + stable Project head."""

    def __init__(self, project_root: Path) -> None:
        self._project_root = project_root

    def _stable_project_bytes(self, retries: int = 3) -> bytes:
        path = self._project_root / "qa/issues/events.jsonl"
        for _attempt in range(retries):
            before = path.read_bytes() if path.exists() else b""
            try:
                read_problem_events_from_bytes(before)
            except LedgerIntegrityError as exc:
                raise IssueHistoryIntegrityError(str(exc)) from exc
            after = path.read_bytes() if path.exists() else b""
            if before == after:
                return before
        raise IssueHistoryConflict("project Problem ledger changed during Retro collection")

    def _read_change_ledger(self, change_id: str) -> tuple[list[ChangeIssueEvent], bytes, Path]:
        try:
            loc = resolve_change(self._project_root, change_id, prefer="archive")
        except ChangeNotFoundError as exc:
            raise IssueHistoryIntegrityError(str(exc)) from exc
        events_path = loc.path / "issues" / "events.jsonl"
        try:
            events = read_change_issue_events(events_path)
        except LedgerIntegrityError as exc:
            raise IssueHistoryIntegrityError(str(exc)) from exc
        raw = events_path.read_bytes() if events_path.exists() else b""
        snapshot_path = loc.path / "issues" / "snapshot.json"
        if snapshot_path.exists() and events:
            expected = dump_projection(project_change_issues(events))
            actual = snapshot_path.read_bytes()
            if actual != expected:
                raise IssueHistoryIntegrityError(
                    f"change {change_id} issue projection mismatch versus authoritative events"
                )
        return events, raw, loc.path

    def _discover_change_ids(self) -> list[str]:
        config_roots = [
            self._project_root / "qa" / "archive",
            self._project_root / "qa" / "changes",
        ]
        found: set[str] = set()
        for root in config_roots:
            if not root.is_dir():
                continue
            for child in root.iterdir():
                if child.is_dir() and (child / "issues" / "events.jsonl").exists():
                    found.add(child.name)
        return sorted(found)

    def read_window(self, selection: IssueWindowSelection) -> IssueEvidenceSlice:
        problem_bytes = self._stable_project_bytes()
        try:
            problem_events = read_problem_events_from_bytes(problem_bytes)
        except LedgerIntegrityError as exc:
            raise IssueHistoryIntegrityError(str(exc)) from exc

        problems_path = self._project_root / "qa" / "issues" / "problems.json"
        if problems_path.exists() and problem_events:
            expected = dump_projection(project_problems(problem_events))
            if problems_path.read_bytes() != expected:
                raise IssueHistoryIntegrityError(
                    "project Problem projection mismatch versus authoritative events"
                )

        time_bounded = selection.event_since is not None or selection.event_until is not None
        if selection.change_ids:
            change_ids = list(selection.change_ids)
        elif time_bounded:
            change_ids = self._discover_change_ids()
        else:
            change_ids = list(selection.change_ids)

        change_events_by_id: dict[str, list[ChangeIssueEvent]] = {}
        change_ledger_bytes: dict[str, bytes] = {}
        for change_id in change_ids:
            events, raw, _path = self._read_change_ledger(change_id)
            change_events_by_id[change_id] = events
            change_ledger_bytes[change_id] = raw

        # Late-review / time-window closure may need additional Changes.
        catalog = dict(change_events_by_id)
        if selection.include_late_review_closure or time_bounded:
            for change_id in self._discover_change_ids():
                if change_id in catalog:
                    continue
                events, raw, _path = self._read_change_ledger(change_id)
                catalog[change_id] = events
                change_ledger_bytes[change_id] = raw

        return build_issue_evidence_slice(
            selection,
            change_events_by_id=change_events_by_id,
            problem_events=problem_events,
            change_ledger_bytes=change_ledger_bytes,
            problem_ledger_bytes=problem_bytes,
            all_change_events_by_id=catalog,
        )
