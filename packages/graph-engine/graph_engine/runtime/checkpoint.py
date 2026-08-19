from __future__ import annotations

import os
from pathlib import Path
from typing import cast
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from graph_engine.canonical import JSONValue, canonical_digest, canonical_json_bytes
from graph_engine.runtime.models import InvocationProjection


_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_STRICT_FROZEN = ConfigDict(frozen=True, extra="forbid", strict=True, allow_inf_nan=False)


class Checkpoint(BaseModel):
    model_config = _STRICT_FROZEN

    last_seq: int = Field(ge=0)
    projection: InvocationProjection
    digest: str = Field(pattern=_SHA256_PATTERN)


def write_checkpoint(path: Path, projection: InvocationProjection, last_seq: int) -> None:
    if not isinstance(last_seq, int) or isinstance(last_seq, bool) or last_seq < 0:
        raise ValueError("last_seq must be a non-negative integer")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _checkpoint_payload(projection, last_seq)
    document = cast(
        JSONValue,
        {
            "last_seq": last_seq,
            "projection": projection.model_dump(mode="json"),
            "digest": canonical_digest(payload),
        },
    )
    pending = path.parent / f".pending-{path.name}-{uuid4().hex}"
    replaced = False
    try:
        with pending.open("xb") as stream:
            stream.write(canonical_json_bytes(document))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(pending, path)
        replaced = True
        _fsync_directory(path.parent)
    finally:
        if not replaced:
            pending.unlink(missing_ok=True)


def load_checkpoint(path: Path, ledger_last_seq: int) -> Checkpoint | None:
    if not isinstance(ledger_last_seq, int) or isinstance(ledger_last_seq, bool) or ledger_last_seq < 0:
        raise ValueError("ledger_last_seq must be a non-negative integer")
    try:
        raw = path.read_bytes()
        checkpoint = Checkpoint.model_validate_json(raw, strict=True)
    except (OSError, UnicodeDecodeError, ValidationError, ValueError):
        return None
    payload = _checkpoint_payload(checkpoint.projection, checkpoint.last_seq)
    if checkpoint.digest != canonical_digest(payload):
        return None
    if checkpoint.last_seq > ledger_last_seq:
        return None
    return checkpoint


def _checkpoint_payload(projection: InvocationProjection, last_seq: int) -> JSONValue:
    return cast(
        JSONValue,
        {
            "last_seq": last_seq,
            "projection": projection.model_dump(mode="json"),
        },
    )


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = ["Checkpoint", "load_checkpoint", "write_checkpoint"]
