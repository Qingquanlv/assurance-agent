"""Strict append-only Project Improvement Ledger store.

Writes use a temp-file + ``os.replace`` pattern for atomicity. Cross-process
serialisation is supplied by callers via ``project:improvement-registry``.

``append_and_rebuild`` is idempotent: events whose ``idempotency_key`` already
appears in the JSONL file are filtered out. When the filtered batch is empty the
call rebuilds projections from the committed ledger and returns them.

Paths (relative to project_root):
    qa/improvements/events.jsonl
    qa/improvements/improvements.json
    qa/improvements/review-queue.json
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Sequence
from pathlib import Path

from assurance_agent.artifacts.models.improvements import ImprovementLedgerProjection
from assurance_agent.workflow.improvements.events import (
    ImprovementEvent,
    read_improvement_events,
)
from assurance_agent.workflow.improvements.projection import (
    dump_projection,
    project_improvement_review_queue,
    project_improvements,
)


def filter_idempotent_events(
    existing: Sequence[ImprovementEvent],
    events: Sequence[ImprovementEvent],
) -> list[ImprovementEvent]:
    """Return only events whose idempotency_key is not already committed."""
    committed = {event.idempotency_key for event in existing}
    return [event for event in events if event.idempotency_key not in committed]


def _serialize_event(event: ImprovementEvent, seq: int) -> str:
    data = dict(event.model_dump(mode="json"))
    data["seq"] = seq
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def atomic_append_jsonl(jsonl_path: Path, new_events: Sequence[ImprovementEvent]) -> None:
    """Append new_events to jsonl_path atomically via temp + os.replace.

    Sequence numbers continue from the number of non-blank lines already present.
    """
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    existing = jsonl_path.read_text(encoding="utf-8") if jsonl_path.exists() else ""
    start_seq = sum(1 for line in existing.splitlines() if line.strip()) + 1

    new_lines = [_serialize_event(event, start_seq + index) for index, event in enumerate(new_events)]

    combined = existing
    if combined and not combined.endswith("\n"):
        combined += "\n"
    combined += "\n".join(new_lines) + "\n"

    dir_path = jsonl_path.parent
    fd, tmp_path = tempfile.mkstemp(dir=dir_path, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(combined)
        os.replace(tmp_path, jsonl_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def atomic_write_json(json_path: Path, data: object) -> None:
    """Write a JSON-serializable mapping (or preformatted bytes) atomically."""
    if isinstance(data, (bytes, bytearray)):
        payload = bytes(data)
    else:
        payload = (
            json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
        ).encode("utf-8")

    json_path.parent.mkdir(parents=True, exist_ok=True)
    dir_path = json_path.parent
    fd, tmp_path = tempfile.mkstemp(dir=dir_path, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
        os.replace(tmp_path, json_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


class ProjectImprovementStore:
    """Append-only ledger for Project Improvement events under ``project_root``."""

    def __init__(self, project_root: Path) -> None:
        self.root = project_root / "qa" / "improvements"
        self._events_path = self.root / "events.jsonl"
        self._improvements_path = self.root / "improvements.json"
        self._queue_path = self.root / "review-queue.json"

    def append_and_rebuild(
        self, events: Sequence[ImprovementEvent]
    ) -> ImprovementLedgerProjection:
        """Append new events and rebuild projections; idempotent on repeat.

        Raises:
            ImprovementLedgerIntegrityError: if the existing ledger is corrupt.
            ValueError: if ``events`` is empty.
            ProjectionError: if new events cannot be applied to the projection.
        """
        if not events:
            raise ValueError("append_and_rebuild requires at least one event")

        existing = read_improvement_events(self._events_path)
        new_events = filter_idempotent_events(existing, events)

        if new_events:
            # Validate the projected result before publishing so a bad batch
            # leaves the on-disk ledger untouched.
            start_seq = (existing[-1].seq + 1) if existing else 1
            sequenced = [
                event.model_copy(update={"seq": start_seq + index})
                for index, event in enumerate(new_events)
            ]
            combined = [*existing, *sequenced]
            projection = project_improvements(combined)
            queue = project_improvement_review_queue(combined)
            atomic_append_jsonl(self._events_path, new_events)
            atomic_write_json(self._improvements_path, dump_projection(projection))
            atomic_write_json(self._queue_path, dump_projection(queue))
            return projection

        committed = read_improvement_events(self._events_path)
        projection = project_improvements(committed)
        queue = project_improvement_review_queue(committed)
        atomic_write_json(self._improvements_path, dump_projection(projection))
        atomic_write_json(self._queue_path, dump_projection(queue))
        return projection
