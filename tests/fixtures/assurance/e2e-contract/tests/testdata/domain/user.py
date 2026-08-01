"""User domain factory stub for the canonical E2E fixture."""

from __future__ import annotations
from typing import Any


async def make_user(**kwargs: Any) -> dict[str, Any]:
    payload = {"username": "limited-user", "role": "limited"}
    payload.update(kwargs)
    return payload


async def cleanup_user(user_id: str) -> None:
    _ = user_id
