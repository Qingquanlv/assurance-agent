"""The sole API exposed to generated verification tests."""

from __future__ import annotations

from collections.abc import Callable

_request: Callable[[str], None] | None = None
_MAX_FRAME = 256 * 1024


def _configure(request: Callable[[str], None]) -> None:
    global _request
    _request = request


def execute_case(case_id: str) -> None:
    """Request the parent to execute the assigned frozen case exactly once."""
    if not isinstance(case_id, str) or not case_id or len(case_id) > 256:
        raise ValueError("case_id must be a nonempty bounded string")
    if _request is None:
        raise RuntimeError("bridge requires the installed confined runner")
    _request(case_id)


__all__ = ["execute_case"]
