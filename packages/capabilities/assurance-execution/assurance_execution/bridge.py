"""The sole API exposed to generated verification tests."""

from __future__ import annotations

import json
from typing import BinaryIO

_reader: BinaryIO | None = None
_writer: BinaryIO | None = None
_MAX_FRAME = 256 * 1024


def _configure(reader: BinaryIO, writer: BinaryIO) -> None:
    global _reader, _writer
    _reader, _writer = reader, writer


def _send(frame: dict[str, object]) -> None:
    if _writer is None:
        raise RuntimeError("bridge requires the installed confined runner")
    payload = json.dumps(frame, separators=(",", ":")).encode() + b"\n"
    if len(payload) > _MAX_FRAME:
        raise RuntimeError("bridge frame exceeds limit")
    _writer.write(payload)
    _writer.flush()


def execute_case(case_id: str) -> None:
    """Request the parent to execute the assigned frozen case exactly once."""
    if not isinstance(case_id, str) or not case_id or len(case_id) > 256:
        raise ValueError("case_id must be a nonempty bounded string")
    if _reader is None:
        raise RuntimeError("bridge requires the installed confined runner")
    _send({"type": "execute", "case_id": case_id})
    payload = _reader.readline(_MAX_FRAME + 1)
    if len(payload) > _MAX_FRAME or not payload.endswith(b"\n"):
        raise RuntimeError("parent bridge closed without acknowledgement")
    response = json.loads(payload)
    if response != {"type": "ack"}:
        raise RuntimeError("parent bridge rejected execution request")


__all__ = ["execute_case"]
