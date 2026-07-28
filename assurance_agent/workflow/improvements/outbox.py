"""Durable, idempotent Improvement reconcile outbox."""

from __future__ import annotations

import os
from pathlib import Path

from assurance_agent.artifacts.canonical import canonical_json_bytes, sha256_bytes
from assurance_agent.artifacts.models.improvement_outbox import ImprovementOutboxEntry
from assurance_agent.exceptions import AaError
from assurance_agent.workflow.improvements.reconciler import (
    ImprovementAcceptStatus,
    reconcile_outbox_entry,
)


class ImprovementOutboxConflict(AaError):
    """An immutable outbox ID already contains different work."""


def _outbox_id(entry: ImprovementOutboxEntry) -> str:
    payload = {
        "retro_id": entry.retro_id,
        "candidate_sha256": entry.candidate_sha256,
        "context_sha256": entry.context_sha256,
    }
    return "OUTBOX-" + sha256_bytes(canonical_json_bytes(payload)).removeprefix("sha256:")[:24]


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temp.write_bytes(data)
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def enqueue_reconcile(project_root: Path, entry: ImprovementOutboxEntry) -> Path:
    """Atomically persist immutable pending reconcile work."""
    path = project_root / "qa" / "improvements" / "outbox" / "pending" / f"{_outbox_id(entry)}.json"
    data = canonical_json_bytes(entry)
    if path.is_file():
        if path.read_bytes() != data:
            raise ImprovementOutboxConflict(f"outbox entry conflicts: {path.name}")
        return path
    _atomic_write(path, data)
    return path


def drain_reconcile_outbox(project_root: Path) -> tuple[ImprovementAcceptStatus, ...]:
    """Reconcile sorted pending entries and durably record each accepted status."""
    pending = project_root / "qa" / "improvements" / "outbox" / "pending"
    if not pending.is_dir():
        return ()
    completed = pending.parent / "completed"
    statuses: list[ImprovementAcceptStatus] = []
    for path in sorted(pending.glob("*.json"), key=lambda item: item.name):
        entry = ImprovementOutboxEntry.model_validate_json(path.read_text(encoding="utf-8"))
        status = reconcile_outbox_entry(project_root, entry)
        completion = completed / path.name
        completion_data = canonical_json_bytes(status)
        if completion.is_file() and completion.read_bytes() != completion_data:
            raise ImprovementOutboxConflict(f"outbox completion conflicts: {path.name}")
        if not completion.is_file():
            _atomic_write(completion, completion_data)
        path.unlink()
        statuses.append(status)
    return tuple(statuses)


__all__ = [
    "ImprovementOutboxConflict",
    "ImprovementOutboxEntry",
    "drain_reconcile_outbox",
    "enqueue_reconcile",
]
