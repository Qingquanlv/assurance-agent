from __future__ import annotations

from typing import Any


def as_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    raise TypeError(f"expected mapping, got {type(value)!r}")
