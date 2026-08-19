"""Parse and order execution batch identifiers across schema generations."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

BATCH_ID_FORMAT_DESCRIPTION = "YYYYMMDD-HHMMSS or YYYYMMDD-HHMMSS-nnnnnnnnn"

_BATCH_ID_RE = re.compile(r"(?P<date>[0-9]{8})-(?P<time>[0-9]{6})(?:-(?P<nanosecond>[0-9]{9}))?")


@dataclass(frozen=True, order=True)
class BatchIdInstant:
    """A UTC second plus its full nanosecond ordering component."""

    timestamp: datetime
    nanosecond: int

    def as_datetime(self) -> datetime:
        """Return the closest Python datetime without losing ordering internally."""
        return self.timestamp + timedelta(microseconds=self.nanosecond // 1_000)


def parse_batch_id(value: str) -> BatchIdInstant | None:
    """Parse legacy second IDs and current nanosecond IDs; reject all other text."""
    match = _BATCH_ID_RE.fullmatch(value)
    if match is None:
        return None
    try:
        timestamp = datetime.strptime(
            f"{match.group('date')}-{match.group('time')}",
            "%Y%m%d-%H%M%S",
        ).replace(tzinfo=UTC)
    except ValueError:
        return None
    return BatchIdInstant(
        timestamp=timestamp,
        nanosecond=int(match.group("nanosecond") or "0"),
    )


def is_valid_batch_id(value: str) -> bool:
    """Return whether ``value`` is a supported, path-safe batch ID shape.

    Execution evidence uses this as a traversal boundary, so it deliberately
    preserves the legacy syntactic contract. Temporal consumers call
    ``parse_batch_id`` and additionally reject impossible calendar values.
    """
    return _BATCH_ID_RE.fullmatch(value) is not None
