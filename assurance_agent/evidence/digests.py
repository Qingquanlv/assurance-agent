"""Shared projection digests and TraceSource recording (fact-only evidence seam)."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime

from assurance_agent.artifacts.models import SelectedTargets
from assurance_agent.artifacts.models.trace import TraceProjectionLike, TraceSource


class TraceSourceConflictError(ValueError):
    """The same source path was observed with conflicting facts."""


class TraceSourceRecorder:
    def __init__(self) -> None:
        self._by_path: dict[str, TraceSource] = {}

    def add(self, source: TraceSource) -> None:
        existing = self._by_path.get(source.path)
        if existing is not None and existing != source:
            raise TraceSourceConflictError(source.path)
        self._by_path[source.path] = source

    def freeze(self) -> tuple[TraceSource, ...]:
        return tuple(self._by_path[path] for path in sorted(self._by_path))


def canonical_json_bytes(obj: object) -> bytes:
    """Deterministic JSON bytes for digests (sorted keys, compact, ensure_ascii default)."""
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        default=_canonical_json_default,
    ).encode("utf-8")


def _canonical_json_default(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, SelectedTargets):
        return value.model_dump()
    raise TypeError(f"unsupported type for canonical JSON: {type(value)!r}")


def projection_digest(projection: TraceProjectionLike) -> str:
    payload = projection.model_dump(mode="json")
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
