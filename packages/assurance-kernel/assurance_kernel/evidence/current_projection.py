"""Point-in-time freshness loader for persisted reconciled Trace projections."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from assurance_kernel.artifacts.models.trace import TraceProjectionV2, load_trace_projection_document
from assurance_kernel.change_location import resolve_change
from assurance_kernel.evidence.digests import projection_digest
from assurance_kernel.evidence.trace import fold_trace

CurrentProjectionStaleReason = Literal[
    "legacy_version",
    "phase_mismatch",
    "change_id_mismatch",
    "batch_id_mismatch",
    "digest_mismatch",
]


class CurrentProjectionError(Exception):
    """Base class for persisted-current projection loading failures."""


class CurrentProjectionMissingError(CurrentProjectionError):
    """The persisted reconciled projection does not exist."""


class CurrentProjectionInvalidError(CurrentProjectionError):
    """The persisted reconciled projection is not valid Trace JSON."""


class CurrentProjectionStaleError(CurrentProjectionError):
    """The persisted projection is not the current reconciled V2 fold."""

    def __init__(self, reason: CurrentProjectionStaleReason) -> None:
        super().__init__(reason)
        self.reason = reason


def load_current_reconciled_projection(
    project_root: Path,
    change_id: str,
) -> TraceProjectionV2:
    path = resolve_change(project_root, change_id).path / "inspect" / "trace-projection.json"
    if not path.is_file():
        raise CurrentProjectionMissingError(str(path))
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CurrentProjectionInvalidError(str(path)) from exc
    if not isinstance(raw, dict):
        raise CurrentProjectionInvalidError(str(path))
    if raw.get("phase") != "reconciled":
        raise CurrentProjectionStaleError("phase_mismatch")
    if raw.get("change_id") != change_id:
        raise CurrentProjectionStaleError("change_id_mismatch")
    live = fold_trace(project_root, change_id, phase="reconciled")
    if raw.get("authoritative_batch_id") != live.authoritative_batch_id:
        raise CurrentProjectionStaleError("batch_id_mismatch")
    try:
        persisted = load_trace_projection_document(raw)
    except ValidationError as exc:
        raise CurrentProjectionInvalidError(str(path)) from exc
    if not isinstance(persisted, TraceProjectionV2):
        raise CurrentProjectionStaleError("legacy_version")
    if projection_digest(persisted) != projection_digest(live):
        raise CurrentProjectionStaleError("digest_mismatch")
    return persisted


__all__ = [
    "CurrentProjectionError",
    "CurrentProjectionInvalidError",
    "CurrentProjectionMissingError",
    "CurrentProjectionStaleError",
    "CurrentProjectionStaleReason",
    "load_current_reconciled_projection",
]
