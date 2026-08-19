from __future__ import annotations

import json
import os
import re
import fcntl
from collections.abc import Sequence
from pathlib import Path
from typing import cast
from uuid import uuid4

from pydantic import TypeAdapter, ValidationError

from graph_engine.canonical import JSONValue, canonical_json_bytes
from graph_engine.errors import GraphEngineError
from graph_engine.runtime.events import EventEnvelope, RuntimeEvent


_FINAL_BATCH = re.compile(r"^(?P<first>[0-9]{10})-(?P<last>[0-9]{10})\.json$")
_BATCH_ADAPTER = TypeAdapter(tuple[EventEnvelope, ...])
MAX_SEQUENCE = 9_999_999_999


class LedgerError(GraphEngineError):
    """Base class for event-ledger failures."""


class LedgerIntegrityError(LedgerError):
    """Raised when an authoritative final ledger batch is invalid."""


class LedgerConflictError(LedgerError):
    """Raised when an append precondition or immutable destination conflicts."""


class Ledger:
    def __init__(self, root: Path) -> None:
        self.root = root

    def append_batch(
        self,
        events: Sequence[RuntimeEvent],
        expected_next_seq: int,
    ) -> tuple[EventEnvelope, ...]:
        if not events:
            raise ValueError("ledger batch must not be empty")
        if (
            not isinstance(expected_next_seq, int)
            or isinstance(expected_next_seq, bool)
            or expected_next_seq < 1
        ):
            raise ValueError("expected_next_seq must be a positive integer")
        last_seq = expected_next_seq + len(events) - 1
        if last_seq > MAX_SEQUENCE:
            raise ValueError(f"ledger batch exceeds maximum sequence {MAX_SEQUENCE}")

        self.root.mkdir(parents=True, exist_ok=True)
        lock_fd = os.open(self.root / ".append.lock", os.O_WRONLY | os.O_CREAT, 0o600)
        pending: Path | None = None
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            _append_boundary("lock_acquired")
            current = self.read_all()
            actual_next_seq = current[-1].seq + 1 if current else 1
            if actual_next_seq != expected_next_seq:
                raise LedgerConflictError(
                    f"expected next sequence {expected_next_seq}, found {actual_next_seq}"
                )

            envelopes = tuple(
                EventEnvelope.from_event(expected_next_seq + offset, event)
                for offset, event in enumerate(events)
            )
            first_seq = envelopes[0].seq
            last_seq = envelopes[-1].seq
            destination = self.root / f"{first_seq:010d}-{last_seq:010d}.json"

            pending = self.root / f".pending-{uuid4().hex}.json"
            document = cast(
                JSONValue,
                [envelope.model_dump(mode="json") for envelope in envelopes],
            )
            with pending.open("xb") as stream:
                stream.write(canonical_json_bytes(document))
                stream.flush()
                os.fsync(stream.fileno())
            _append_boundary("pending_fsynced")

            _publish_no_clobber(pending, destination)
            _append_boundary("final_installed")
            _fsync_directory(self.root)
            _append_boundary("directory_fsynced")
            pending.unlink()
            pending = None
            _fsync_directory(self.root)
            return envelopes
        finally:
            if pending is not None:
                pending.unlink(missing_ok=True)
            os.close(lock_fd)

    def read_all(self) -> tuple[EventEnvelope, ...]:
        if not self.root.exists():
            return ()
        if not self.root.is_dir():
            raise LedgerIntegrityError(f"ledger root is not a directory: {self.root}")

        final_batches: list[tuple[int, int, Path]] = []
        for path in self.root.iterdir():
            if path.name == ".append.lock":
                continue
            if path.name.startswith(".pending-"):
                continue
            match = _FINAL_BATCH.fullmatch(path.name)
            if match is None or not path.is_file():
                raise LedgerIntegrityError(f"invalid final batch filename: {path.name}")
            first_seq = int(match.group("first"))
            last_seq = int(match.group("last"))
            if first_seq < 1 or last_seq < first_seq:
                raise LedgerIntegrityError(f"invalid sequence range in final batch filename: {path.name}")
            final_batches.append((first_seq, last_seq, path))

        final_batches.sort(key=lambda item: (item[0], item[1], item[2].name))
        expected_seq = 1
        result: list[EventEnvelope] = []
        for first_seq, last_seq, path in final_batches:
            if first_seq != expected_seq:
                raise LedgerIntegrityError(f"expected batch starting at {expected_seq}, found {path.name}")
            envelopes = _read_batch(path)
            actual_range = (envelopes[0].seq, envelopes[-1].seq)
            if actual_range != (first_seq, last_seq):
                raise LedgerIntegrityError(
                    f"batch filename {path.name} does not match envelope range "
                    f"{actual_range[0]}-{actual_range[1]}"
                )
            for envelope in envelopes:
                if envelope.seq != expected_seq:
                    raise LedgerIntegrityError(
                        f"expected envelope sequence {expected_seq}, found {envelope.seq} in {path.name}"
                    )
                if not envelope.has_valid_digest():
                    raise LedgerIntegrityError(
                        f"event envelope {envelope.seq} digest mismatch in {path.name}"
                    )
                result.append(envelope)
                expected_seq += 1
        return tuple(result)


def _read_batch(path: Path) -> tuple[EventEnvelope, ...]:
    try:
        raw = path.read_bytes()
        decoded = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LedgerIntegrityError(f"malformed JSON in final batch {path.name}") from error
    if not isinstance(decoded, list):
        raise LedgerIntegrityError(f"final batch {path.name} must contain a JSON array")
    if not decoded:
        raise LedgerIntegrityError(f"final batch {path.name} must contain at least one envelope")
    try:
        return _BATCH_ADAPTER.validate_json(raw, strict=True)
    except ValidationError as error:
        raise LedgerIntegrityError(f"invalid event envelope in final batch {path.name}: {error}") from error


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _publish_no_clobber(pending: Path, destination: Path) -> None:
    try:
        os.link(pending, destination)
    except FileExistsError as error:
        raise LedgerConflictError(f"immutable ledger batch already exists: {destination.name}") from error


def _append_boundary(name: str) -> None:
    del name


__all__ = [
    "Ledger",
    "LedgerConflictError",
    "LedgerError",
    "LedgerIntegrityError",
    "MAX_SEQUENCE",
]
