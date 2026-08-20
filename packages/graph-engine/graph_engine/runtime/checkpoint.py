from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path
from typing import cast
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from graph_engine.canonical import JSONValue, canonical_digest, canonical_json_bytes
from graph_engine.runtime.events import EventEnvelope
from graph_engine.runtime.models import InvocationProjection, ProjectionError, fold_events


_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_STRICT_FROZEN = ConfigDict(frozen=True, extra="forbid", strict=True, allow_inf_nan=False)


class Checkpoint(BaseModel):
    model_config = _STRICT_FROZEN

    last_seq: int = Field(ge=0)
    ledger_prefix_sha256: str = Field(pattern=_SHA256_PATTERN)
    projection: InvocationProjection
    digest: str = Field(pattern=_SHA256_PATTERN)


def write_checkpoint(
    path: Path,
    projection: InvocationProjection,
    last_seq: int,
    *,
    ledger_envelopes: Sequence[EventEnvelope],
) -> None:
    document = _checkpoint_bytes(projection, last_seq, ledger_envelopes)
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.parent / f".pending-{path.name}-{uuid4().hex}"
    replaced = False
    try:
        with pending.open("xb") as stream:
            stream.write(document)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(pending, path)
        replaced = True
        _fsync_directory(path.parent)
    finally:
        if not replaced:
            pending.unlink(missing_ok=True)


def write_checkpoint_at(
    parent_fd: int,
    name: str,
    projection: InvocationProjection,
    last_seq: int,
    *,
    ledger_envelopes: Sequence[EventEnvelope],
) -> None:
    document = _checkpoint_bytes(projection, last_seq, ledger_envelopes)
    pending = f".pending-{name}-{uuid4().hex}"
    descriptor = os.open(
        pending,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
        dir_fd=parent_fd,
    )
    try:
        try:
            view = memoryview(document)
            while view:
                written = os.write(descriptor, view)
                if written == 0:
                    raise OSError("checkpoint write returned zero bytes")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(pending, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        os.fsync(parent_fd)
    finally:
        try:
            os.unlink(pending, dir_fd=parent_fd)
        except FileNotFoundError:
            pass


def _checkpoint_bytes(
    projection: InvocationProjection,
    last_seq: int,
    ledger_envelopes: Sequence[EventEnvelope],
) -> bytes:
    if not isinstance(last_seq, int) or isinstance(last_seq, bool) or last_seq < 0:
        raise ValueError("last_seq must be a non-negative integer")
    envelopes = tuple(ledger_envelopes)
    prefix_digest = _verified_prefix_digest(envelopes)
    actual_last_seq = envelopes[-1].seq if envelopes else 0
    if last_seq != actual_last_seq:
        raise ValueError(f"checkpoint last_seq {last_seq} does not match ledger prefix {actual_last_seq}")
    try:
        replayed = fold_events(envelopes)
    except ProjectionError as error:
        raise ValueError("cannot checkpoint an invalid ledger prefix") from error
    if replayed != projection:
        raise ValueError("checkpoint projection does not match ledger prefix")
    payload = _checkpoint_payload(projection, last_seq, prefix_digest)
    document = cast(
        JSONValue,
        {
            "last_seq": last_seq,
            "ledger_prefix_sha256": prefix_digest,
            "projection": projection.model_dump(mode="json"),
            "digest": canonical_digest(payload),
        },
    )
    return canonical_json_bytes(document)


def load_checkpoint(path: Path, *, ledger_envelopes: Sequence[EventEnvelope]) -> Checkpoint | None:
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    return _load_checkpoint_bytes(raw, ledger_envelopes)


def load_checkpoint_at(
    parent_fd: int,
    name: str,
    *,
    ledger_envelopes: Sequence[EventEnvelope],
) -> Checkpoint | None:
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
        try:
            chunks: list[bytes] = []
            while chunk := os.read(descriptor, 1024 * 1024):
                chunks.append(chunk)
            raw = b"".join(chunks)
        finally:
            os.close(descriptor)
    except OSError:
        return None
    return _load_checkpoint_bytes(raw, ledger_envelopes)


def _load_checkpoint_bytes(
    raw: bytes,
    ledger_envelopes: Sequence[EventEnvelope],
) -> Checkpoint | None:
    try:
        checkpoint = Checkpoint.model_validate_json(raw, strict=True)
    except (UnicodeDecodeError, ValidationError, ValueError):
        return None
    payload = _checkpoint_payload(
        checkpoint.projection,
        checkpoint.last_seq,
        checkpoint.ledger_prefix_sha256,
    )
    if checkpoint.digest != canonical_digest(payload):
        return None
    envelopes = tuple(ledger_envelopes)
    actual_last_seq = envelopes[-1].seq if envelopes else 0
    if checkpoint.last_seq != actual_last_seq:
        return None
    try:
        prefix_digest = _verified_prefix_digest(envelopes)
        replayed = fold_events(envelopes)
    except (ProjectionError, ValueError):
        return None
    if checkpoint.ledger_prefix_sha256 != prefix_digest or checkpoint.projection != replayed:
        return None
    return checkpoint


def _checkpoint_payload(
    projection: InvocationProjection,
    last_seq: int,
    ledger_prefix_sha256: str,
) -> JSONValue:
    return cast(
        JSONValue,
        {
            "last_seq": last_seq,
            "ledger_prefix_sha256": ledger_prefix_sha256,
            "projection": projection.model_dump(mode="json"),
        },
    )


def _verified_prefix_digest(envelopes: tuple[EventEnvelope, ...]) -> str:
    for expected_seq, envelope in enumerate(envelopes, start=1):
        if envelope.seq != expected_seq or not envelope.has_valid_digest():
            raise ValueError("ledger prefix is not contiguous and digest-valid")
    document = cast(JSONValue, [item.model_dump(mode="json") for item in envelopes])
    return canonical_digest(document)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "Checkpoint",
    "load_checkpoint",
    "load_checkpoint_at",
    "write_checkpoint",
    "write_checkpoint_at",
]
