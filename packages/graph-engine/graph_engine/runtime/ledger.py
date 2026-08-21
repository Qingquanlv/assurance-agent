from __future__ import annotations

import json
import os
import re
import fcntl
import stat
from collections.abc import Sequence
from pathlib import Path
from typing import cast
from uuid import uuid4

from pydantic import TypeAdapter, ValidationError

from graph_engine.canonical import JSONValue, canonical_json_bytes
from graph_engine.errors import GraphEngineError
from graph_engine.runtime.events import EventEnvelope, RuntimeEvent
from graph_engine.runtime.models import fold_events


_FINAL_BATCH = re.compile(r"^(?P<first>[0-9]{10})-(?P<last>[0-9]{10})\.json$")
_BATCH_ADAPTER = TypeAdapter(tuple[EventEnvelope, ...])
MAX_SEQUENCE = 9_999_999_999
_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
_FILE_READ_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)


class LedgerError(GraphEngineError):
    """Base class for event-ledger failures."""


class LedgerIntegrityError(LedgerError):
    """Raised when an authoritative final ledger batch is invalid."""


class LedgerConflictError(LedgerError):
    """Raised when an append precondition or immutable destination conflicts."""


class LedgerPublicationIndeterminate(LedgerError):
    """Raised when an append outcome cannot be established from the exact range."""


class Ledger:
    def __init__(self, root: Path, *, _parent_fd: int | None = None) -> None:
        self.root = root
        self._parent_fd = _parent_fd
        self.boundaries: list[str] = []

    @classmethod
    def at(cls, parent_fd: int, name: str, *, display_root: Path) -> Ledger:
        if not name or "/" in name or name in {".", ".."}:
            raise ValueError("ledger name must be one path component")
        root = Path(display_root)
        if root.name != name:
            raise ValueError("display root must end with the ledger name")
        return cls(root, _parent_fd=parent_fd)

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

        root_fd = self._open_root(create=True)
        lock_fd = os.open(
            ".append.lock",
            os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=root_fd,
        )
        if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
            os.close(lock_fd)
            os.close(root_fd)
            raise LedgerIntegrityError("ledger append lock is not a regular file")
        pending: str | None = None
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            _append_boundary("lock_acquired")
            current = self._read_all_fd(root_fd)
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
            destination = f"{first_seq:010d}-{last_seq:010d}.json"

            pending = f".pending-{uuid4().hex}.json"
            document = cast(
                JSONValue,
                [envelope.model_dump(mode="json") for envelope in envelopes],
            )
            pending_fd = os.open(
                pending,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=root_fd,
            )
            try:
                _write_all(pending_fd, canonical_json_bytes(document))
                os.fsync(pending_fd)
            finally:
                os.close(pending_fd)
            _append_boundary("pending_fsynced")

            _publish_no_clobber_at(root_fd, pending, destination)
            _append_boundary("final_installed")
            os.fsync(root_fd)
            _append_boundary("directory_fsynced")
            os.unlink(pending, dir_fd=root_fd)
            pending = None
            os.fsync(root_fd)
            self.boundaries.append("batch_append")
            return envelopes
        finally:
            if pending is not None:
                try:
                    os.unlink(pending, dir_fd=root_fd)
                except FileNotFoundError:
                    pass
            os.close(lock_fd)
            os.close(root_fd)

    def read_all(self) -> tuple[EventEnvelope, ...]:
        try:
            root_fd = self._open_root(create=False)
        except FileNotFoundError:
            return ()
        try:
            return self._read_all_fd(root_fd)
        finally:
            os.close(root_fd)

    def read_bytes(self) -> bytes:
        """Return concatenated raw bytes of authenticated final batches."""
        try:
            root_fd = self._open_root(create=False)
        except FileNotFoundError:
            return b""
        try:
            self._read_all_fd(root_fd)
            names = [name for name in os.listdir(root_fd) if _FINAL_BATCH.fullmatch(name) is not None]
            names.sort()
            return b"".join(_read_raw_at(root_fd, name) for name in names)
        finally:
            os.close(root_fd)

    def read_bootstrap(self) -> tuple[EventEnvelope, ...]:
        """Read and authenticate only the unique sequence-one batch."""
        try:
            root_fd = self._open_root(create=False)
        except FileNotFoundError:
            return ()
        try:
            candidates: list[str] = []
            try:
                names = os.listdir(root_fd)
            except OSError as error:
                raise LedgerIntegrityError("cannot enumerate ledger directory") from error
            for name in names:
                match = _FINAL_BATCH.fullmatch(name)
                if match is not None and int(match.group("first")) == 1:
                    candidates.append(name)
            if len(candidates) != 1:
                raise LedgerIntegrityError("ledger must contain one canonical bootstrap batch")
            name = candidates[0]
            envelopes = _read_batch_at(root_fd, name)
            match = _FINAL_BATCH.fullmatch(name)
            assert match is not None
            if (envelopes[0].seq, envelopes[-1].seq) != (
                int(match.group("first")),
                int(match.group("last")),
            ):
                raise LedgerIntegrityError("canonical bootstrap batch range is invalid")
            for expected_seq, envelope in enumerate(envelopes, start=1):
                if envelope.seq != expected_seq or not envelope.has_valid_digest():
                    raise LedgerIntegrityError("canonical bootstrap batch is invalid")
            return envelopes
        finally:
            os.close(root_fd)

    def ensure_durable(self) -> None:
        """Establish a durability barrier for every currently visible ledger entry."""
        root_fd = self._open_root(create=False)
        try:
            os.fsync(root_fd)
        finally:
            os.close(root_fd)

    def _read_all_fd(self, root_fd: int) -> tuple[EventEnvelope, ...]:
        if not stat.S_ISDIR(os.fstat(root_fd).st_mode):
            raise LedgerIntegrityError(f"ledger root is not a directory: {self.root}")

        final_batches: list[tuple[int, int, str]] = []
        try:
            names = os.listdir(root_fd)
        except OSError as error:
            raise LedgerIntegrityError("cannot enumerate ledger directory") from error
        for name in names:
            if name == ".append.lock":
                continue
            if name.startswith(".pending-"):
                continue
            match = _FINAL_BATCH.fullmatch(name)
            try:
                entry = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
            except OSError as error:
                raise LedgerIntegrityError(f"cannot inspect ledger entry: {name}") from error
            if match is None or not stat.S_ISREG(entry.st_mode):
                raise LedgerIntegrityError(f"invalid final batch filename: {name}")
            first_seq = int(match.group("first"))
            last_seq = int(match.group("last"))
            if first_seq < 1 or last_seq < first_seq:
                raise LedgerIntegrityError(f"invalid sequence range in final batch filename: {name}")
            final_batches.append((first_seq, last_seq, name))

        final_batches.sort(key=lambda item: (item[0], item[1], item[2]))
        expected_seq = 1
        result: list[EventEnvelope] = []
        for first_seq, last_seq, name in final_batches:
            if first_seq != expected_seq:
                raise LedgerIntegrityError(f"expected batch starting at {expected_seq}, found {name}")
            envelopes = _read_batch_at(root_fd, name)
            actual_range = (envelopes[0].seq, envelopes[-1].seq)
            if actual_range != (first_seq, last_seq):
                raise LedgerIntegrityError(
                    f"batch filename {name} does not match envelope range {actual_range[0]}-{actual_range[1]}"
                )
            for envelope in envelopes:
                if envelope.seq != expected_seq:
                    raise LedgerIntegrityError(
                        f"expected envelope sequence {expected_seq}, found {envelope.seq} in {name}"
                    )
                if not envelope.has_valid_digest():
                    raise LedgerIntegrityError(f"event envelope {envelope.seq} digest mismatch in {name}")
                result.append(envelope)
                expected_seq += 1
        return tuple(result)

    def _open_root(self, *, create: bool) -> int:
        if self._parent_fd is not None:
            parent_fd = os.dup(self._parent_fd)
            name = self.root.name
        else:
            if create:
                self.root.parent.mkdir(parents=True, exist_ok=True)
            parent_fd = _open_absolute_directory(self.root.parent)
            name = self.root.name
        try:
            if create:
                try:
                    os.mkdir(name, mode=0o700, dir_fd=parent_fd)
                    os.fsync(parent_fd)
                except FileExistsError:
                    pass
            return _open_directory_at(parent_fd, name, "ledger root")
        finally:
            os.close(parent_fd)


def append_validated_batch(
    ledger: Ledger,
    events: Sequence[RuntimeEvent],
    *,
    expected_next_seq: int,
) -> tuple[EventEnvelope, ...]:
    """Fold, compare-and-append, then reconcile only the prospective exact range."""
    materialized = tuple(events)
    if not materialized:
        raise ValueError("ledger batch must not be empty")
    existing = ledger.read_all()
    actual_next_seq = existing[-1].seq + 1 if existing else 1
    if actual_next_seq != expected_next_seq:
        raise LedgerConflictError(f"expected next sequence {expected_next_seq}, found {actual_next_seq}")
    expected = tuple(
        EventEnvelope.from_event(expected_next_seq + offset, event)
        for offset, event in enumerate(materialized)
    )
    fold_events(existing + expected)
    _validated_append_boundary("before", materialized)
    try:
        published = ledger.append_batch(materialized, expected_next_seq=expected_next_seq)
    except LedgerConflictError:
        raise
    except BaseException:
        try:
            persisted = ledger.read_all()
        except BaseException as reconciliation_error:
            raise LedgerPublicationIndeterminate(
                "ledger publication outcome is indeterminate"
            ) from reconciliation_error
        offset = expected_next_seq - 1
        if persisted[offset : offset + len(expected)] == expected:
            try:
                ledger.ensure_durable()
            except BaseException as durability_error:
                raise LedgerPublicationIndeterminate(
                    "ledger range is visible but its directory durability is indeterminate"
                ) from durability_error
            published = expected
        else:
            raise
    _validated_append_boundary("after", materialized)
    return published


def _read_raw_at(root_fd: int, name: str) -> bytes:
    descriptor: int | None = None
    try:
        descriptor = os.open(name, _FILE_READ_FLAGS, dir_fd=root_fd)
        opened = os.fstat(descriptor)
        enumerated = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
            enumerated.st_dev,
            enumerated.st_ino,
        ):
            raise LedgerIntegrityError(f"final batch is not a stable regular file: {name}")
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        return b"".join(chunks)
    except OSError as error:
        raise LedgerIntegrityError(f"malformed JSON in final batch {name}") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _read_batch_at(root_fd: int, name: str) -> tuple[EventEnvelope, ...]:
    try:
        raw = _read_raw_at(root_fd, name)
        decoded = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, LedgerIntegrityError) as error:
        if isinstance(error, LedgerIntegrityError):
            raise
        raise LedgerIntegrityError(f"malformed JSON in final batch {name}") from error
    if not isinstance(decoded, list):
        raise LedgerIntegrityError(f"final batch {name} must contain a JSON array")
    if not decoded:
        raise LedgerIntegrityError(f"final batch {name} must contain at least one envelope")
    try:
        return _BATCH_ADAPTER.validate_json(raw, strict=True)
    except ValidationError as error:
        raise LedgerIntegrityError(f"invalid event envelope in final batch {name}: {error}") from error


def _open_absolute_directory(path: Path) -> int:
    absolute = path.absolute()
    descriptor = os.open("/", _DIRECTORY_FLAGS)
    try:
        for component in absolute.parts[1:]:
            child = _open_directory_at(descriptor, component, "directory path component")
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_directory_at(parent_fd: int, name: str, kind: str) -> int:
    try:
        enumerated = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError:
        raise
    except OSError as error:
        raise LedgerIntegrityError(f"{kind} must be a no-follow directory: {name}") from error
    opened = os.fstat(descriptor)
    if not stat.S_ISDIR(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
        enumerated.st_dev,
        enumerated.st_ino,
    ):
        os.close(descriptor)
        raise LedgerIntegrityError(f"{kind} identity changed while opening: {name}")
    return descriptor


def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(descriptor, view)
        if written == 0:
            raise LedgerError("filesystem write returned zero bytes")
        view = view[written:]


def _publish_no_clobber_at(root_fd: int, pending: str, destination: str) -> None:
    try:
        os.link(
            pending,
            destination,
            src_dir_fd=root_fd,
            dst_dir_fd=root_fd,
            follow_symlinks=False,
        )
    except FileExistsError as error:
        raise LedgerConflictError(f"immutable ledger batch already exists: {destination}") from error


def _publish_no_clobber(pending: Path, destination: Path) -> None:
    """Compatibility seam used by the cross-process no-clobber regression."""
    try:
        os.link(pending, destination, follow_symlinks=False)
    except FileExistsError as error:
        raise LedgerConflictError(f"immutable ledger batch already exists: {destination.name}") from error


def _append_boundary(name: str) -> None:
    del name


def _validated_append_boundary(phase: str, events: Sequence[RuntimeEvent]) -> None:
    del phase, events


__all__ = [
    "Ledger",
    "LedgerConflictError",
    "LedgerError",
    "LedgerIntegrityError",
    "LedgerPublicationIndeterminate",
    "MAX_SEQUENCE",
    "append_validated_batch",
]
