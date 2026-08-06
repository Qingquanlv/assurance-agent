"""Persist / load immutable C3 replay attempt receipts (workflow I/O)."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from pydantic import ValidationError

from assurance_agent.artifacts.models.discovery import ReplayAttemptReceipt
from assurance_agent.evidence.replay_telemetry import (
    REPLAY_RECEIPT_DIR_REL,
    replay_receipt_relpath,
)
from assurance_agent.workflow.execution.evidence import atomic_write_bytes


class ReplayReceiptIntegrityError(ValueError):
    """A persisted replay attempt is corrupt or conflicts with its identity."""


def write_replay_attempt_receipts(
    change_dir: Path,
    receipts: Sequence[ReplayAttemptReceipt],
) -> tuple[Path, ...]:
    """Write one JSON receipt per attempt under discovery (immutable overwrite)."""
    written: list[Path] = []
    for receipt in receipts:
        rel = replay_receipt_relpath(
            counterexample_id=receipt.counterexample_id,
            attempt_index=receipt.attempt_index,
        )
        path = Path(change_dir) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = (json.dumps(receipt.model_dump(mode="json"), indent=2, sort_keys=True) + "\n").encode(
            "utf-8"
        )
        if path.exists():
            try:
                existing = path.read_bytes()
            except OSError as err:
                raise ReplayReceiptIntegrityError(
                    f"immutable replay receipt {rel} is unreadable: {err}"
                ) from err
            if existing != payload:
                raise ReplayReceiptIntegrityError(
                    f"immutable replay receipt {rel} already exists with different bytes"
                )
        else:
            atomic_write_bytes(path, payload)
        written.append(path)
    return tuple(written)


def load_replay_attempt_receipts(
    change_dir: Path,
    counterexample_id: str | None = None,
) -> tuple[ReplayAttemptReceipt, ...]:
    """Load immutable receipts from discovery.

    When ``counterexample_id`` is set, only that CE's receipts are returned.
    Missing receipt directories return ``()``. A present corrupt receipt raises
    :class:`ReplayReceiptIntegrityError` so callers cannot inflate replay rate
    by silently dropping failed/corrupt attempts.
    """
    root = Path(change_dir) / REPLAY_RECEIPT_DIR_REL
    if not root.is_dir():
        return ()

    if counterexample_id is not None:
        replay_dirs = [root / counterexample_id / "replay"]
    else:
        replay_dirs = sorted(p / "replay" for p in root.iterdir() if p.is_dir())

    loaded: list[ReplayAttemptReceipt] = []
    for replay_dir in replay_dirs:
        if not replay_dir.is_dir():
            continue
        for path in sorted(replay_dir.glob("attempt-*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                loaded.append(ReplayAttemptReceipt.model_validate(data))
            except (OSError, ValueError, ValidationError, json.JSONDecodeError) as err:
                rel = path.relative_to(change_dir).as_posix()
                raise ReplayReceiptIntegrityError(f"replay receipt {rel} is invalid: {err}") from err
    return tuple(loaded)


__all__ = [
    "ReplayReceiptIntegrityError",
    "load_replay_attempt_receipts",
    "write_replay_attempt_receipts",
]
