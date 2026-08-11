"""Role domain factory stub for the canonical E2E fixture."""

from __future__ import annotations
from typing import Any


async def make_role(**kwargs: Any) -> dict[str, Any]:
    payload = {"name": "limited", "permissions": []}
    payload.update(kwargs)
    return payload


async def cleanup_role(role_id: str) -> None:
    _ = role_id
