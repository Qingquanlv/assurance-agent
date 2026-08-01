"""E2E auth adapter stub for the canonical E2E fixture."""

from __future__ import annotations

async def e2e_seed_limited_user(**kwargs):
    return {"username": "limited-user", **kwargs}

async def e2e_cleanup_limited_user(user_id: str) -> None:
    _ = user_id
