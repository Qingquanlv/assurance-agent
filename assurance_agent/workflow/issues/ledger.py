"""Strict append-only Ledger stores for Change Issue and Project Problem events.

The task workspace is the transaction boundary.  Writes use a temp-file +
``os.replace`` pattern for atomicity.  No additional filesystem locks are
acquired here; cross-process serialisation is supplied by Task 4's
synchronized graph resource (``project:issue-registry``).

``append_and_rebuild`` is idempotent: if every event in the batch has an
idempotency_key that already appears in the JSONL file the call is a no-op
and returns the rebuilt projection from the committed events.

Atomicity guarantee: either all new events are committed and projections are
written, or no change is made.  A partial failure leaves the original files
untouched.

Paths (relative to workspace_root):
    Change:  issues/events.jsonl   +  issues/snapshot.json
    Project: qa/issues/events.jsonl + qa/issues/problems.json
                                   + qa/issues/review-queue.json
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from assurance_agent.artifacts.models.issues import (
    ChangeIssueSnapshot,
    ProblemProjection,
    ProblemReviewQueue,
)
from assurance_agent.workflow.issues.events import (
    ChangeIssueEvent,
    ProblemEvent,
    read_change_issue_events,
    read_problem_events,
)
from assurance_agent.workflow.issues.projection import (
    dump_projection,
    project_change_issues,
    project_problems,
    project_review_queue,
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


_EventT = TypeVar("_EventT", bound=BaseModel)


def _canonical_payload_bytes(event: BaseModel) -> bytes:
    data = dict(event.model_dump(mode="json"))
    data.pop("seq", None)
    data.pop("ts", None)
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _event_identity(event: BaseModel) -> tuple[str, str]:
    data = event.model_dump(mode="json", include={"event_id", "idempotency_key"})
    event_id = data.get("event_id")
    idempotency_key = data.get("idempotency_key")
    if not isinstance(event_id, str) or not isinstance(idempotency_key, str):
        raise TypeError("ledger event lacks event_id or idempotency_key")
    return event_id, idempotency_key


def _next_seq(path: Path) -> int:
    """Return the next sequence number for the JSONL file."""
    if not path.exists():
        return 1
    count = 0
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if raw_line.strip():
            count += 1
    return count + 1


def _serialize_event(event: object, seq: int) -> str:
    """Dump a pydantic event model as a JSONL line with the assigned seq."""
    data: dict[str, object]
    if hasattr(event, "model_dump"):
        data = dict(event.model_dump(mode="json"))  # type: ignore[union-attr]
    else:
        data = dict(event)  # type: ignore[arg-type]
    data["seq"] = seq
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _atomic_append(
    jsonl_path: Path,
    new_events: Sequence[object],
    start_seq: int,
) -> None:
    """Append new_events to jsonl_path atomically via temp + os.replace.

    Reads the current file content, appends the new lines, writes to a
    sibling temp file, then replaces the original atomically.
    """
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)

    existing = jsonl_path.read_text(encoding="utf-8") if jsonl_path.exists() else ""

    new_lines: list[str] = []
    for i, event in enumerate(new_events):
        new_lines.append(_serialize_event(event, start_seq + i))

    combined = existing
    if combined and not combined.endswith("\n"):
        combined += "\n"
    combined += "\n".join(new_lines) + "\n"

    dir_path = jsonl_path.parent
    fd, tmp_path = tempfile.mkstemp(dir=dir_path, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(combined)
        os.replace(tmp_path, jsonl_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _atomic_write_json(json_path: Path, data: bytes) -> None:
    """Write bytes to json_path atomically via temp + os.replace."""
    json_path.parent.mkdir(parents=True, exist_ok=True)
    dir_path = json_path.parent
    fd, tmp_path = tempfile.mkstemp(dir=dir_path, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp_path, json_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _filter_new_events(
    committed: Sequence[_EventT],
    incoming: Sequence[_EventT],
) -> list[_EventT]:
    """Filter semantic retries and reject identity/payload conflicts.

    ``seq`` is assigned by the store and retrying producers generate a fresh
    envelope ``ts``. Neither field changes the semantic event bound to its
    deterministic event ID and idempotency key.
    """
    from assurance_agent.workflow.issues.events import LedgerIntegrityError

    committed_by_event_id: dict[str, bytes] = {}
    committed_by_key: dict[str, bytes] = {}
    for event in committed:
        event_id, key = _event_identity(event)
        payload = _canonical_payload_bytes(event)
        committed_by_event_id[event_id] = payload
        committed_by_key[key] = payload

    seen_by_event_id: dict[str, bytes] = {}
    seen_by_key: dict[str, bytes] = {}
    filtered: list[_EventT] = []
    for event in incoming:
        event_id, key = _event_identity(event)
        payload = _canonical_payload_bytes(event)

        prior_event = committed_by_event_id.get(event_id)
        if prior_event is not None:
            if prior_event != payload:
                raise LedgerIntegrityError(f"duplicate event_id {event_id!r} with different event payload")
            continue

        prior_key = committed_by_key.get(key)
        if prior_key is not None:
            if prior_key != payload:
                raise LedgerIntegrityError(
                    f"idempotency conflict for key {key!r}: same key with different event payload"
                )
            continue

        batch_event = seen_by_event_id.get(event_id)
        if batch_event is not None:
            if batch_event != payload:
                raise LedgerIntegrityError(f"duplicate event_id {event_id!r} with different event payload")
            continue

        batch_key = seen_by_key.get(key)
        if batch_key is not None:
            if batch_key != payload:
                raise LedgerIntegrityError(
                    f"idempotency conflict for key {key!r}: same key with different event payload"
                )
            continue

        seen_by_event_id[event_id] = payload
        seen_by_key[key] = payload
        filtered.append(event)
    return filtered


# ---------------------------------------------------------------------------
# ChangeIssueStore
# ---------------------------------------------------------------------------


class ChangeIssueStore:
    """Append-only ledger for Change Issue events stored under ``workspace_root``.

    Storage layout::

        {workspace_root}/issues/events.jsonl   — ordered JSONL event log
        {workspace_root}/issues/snapshot.json  — rebuilt projection

    ``workspace_root`` is typically the task workspace directory (not the
    project root).  Cross-process locks are NOT added here; callers must
    ensure mutual exclusion via the graph resource system.
    """

    _EVENTS_RELPATH = "issues/events.jsonl"
    _SNAPSHOT_RELPATH = "issues/snapshot.json"

    def __init__(self, workspace_root: Path) -> None:
        self._root = workspace_root
        self._events_path = workspace_root / self._EVENTS_RELPATH
        self._snapshot_path = workspace_root / self._SNAPSHOT_RELPATH

    def append_and_rebuild(self, events: Sequence[ChangeIssueEvent]) -> ChangeIssueSnapshot:
        """Append new events and rebuild the snapshot; idempotent on repeat.

        If every event in ``events`` has an idempotency_key already present in
        the JSONL file the call is a no-op and returns the snapshot rebuilt
        from the committed events.

        Raises:
            LedgerIntegrityError: if the existing ledger is corrupt.
            ValueError: if ``events`` is empty.
        """
        if not events:
            raise ValueError("append_and_rebuild requires at least one event")

        committed = read_change_issue_events(self._events_path)
        new_events = _filter_new_events(committed, events)

        if new_events:
            # Validate seq continuity won't break; assign fresh seq numbers.
            start_seq = _next_seq(self._events_path)
            _atomic_append(self._events_path, new_events, start_seq)

        # Re-read and validate the full ledger, then rebuild.
        committed = read_change_issue_events(self._events_path)
        snapshot = project_change_issues(committed)
        _atomic_write_json(self._snapshot_path, dump_projection(snapshot))
        return snapshot


# ---------------------------------------------------------------------------
# ProjectProblemStore
# ---------------------------------------------------------------------------


class ProjectProblemStore:
    """Append-only ledger for Project Problem events stored under ``workspace_root``.

    Storage layout::

        {workspace_root}/qa/issues/events.jsonl    — ordered JSONL event log
        {workspace_root}/qa/issues/problems.json   — rebuilt Problem projection
        {workspace_root}/qa/issues/review-queue.json — rebuilt review queue

    ``workspace_root`` is typically the task workspace directory.
    """

    _EVENTS_RELPATH = "qa/issues/events.jsonl"
    _PROBLEMS_RELPATH = "qa/issues/problems.json"
    _QUEUE_RELPATH = "qa/issues/review-queue.json"

    def __init__(self, workspace_root: Path) -> None:
        self._root = workspace_root
        self._events_path = workspace_root / self._EVENTS_RELPATH
        self._problems_path = workspace_root / self._PROBLEMS_RELPATH
        self._queue_path = workspace_root / self._QUEUE_RELPATH

    def append_and_rebuild(
        self, events: Sequence[ProblemEvent]
    ) -> tuple[ProblemProjection, ProblemReviewQueue]:
        """Append new events and rebuild problem/queue projections; idempotent.

        If every event in ``events`` has an idempotency_key already present in
        the JSONL file the call is a no-op and returns projections rebuilt from
        the committed events.

        The append and both projection writes are performed atomically as a
        group: the temp files are all written before any ``os.replace`` call,
        so a mid-write failure leaves all three originals intact.

        Raises:
            LedgerIntegrityError: if the existing ledger is corrupt.
            ValueError: if ``events`` is empty.
        """
        if not events:
            raise ValueError("append_and_rebuild requires at least one event")

        committed = read_problem_events(self._events_path)
        new_events = _filter_new_events(committed, events)

        if new_events:
            start_seq = _next_seq(self._events_path)
            _atomic_append(self._events_path, new_events, start_seq)

        committed = read_problem_events(self._events_path)
        projection = project_problems(committed)
        queue = project_review_queue(committed)

        _atomic_write_json(self._problems_path, dump_projection(projection))
        _atomic_write_json(self._queue_path, dump_projection(queue))
        return projection, queue
