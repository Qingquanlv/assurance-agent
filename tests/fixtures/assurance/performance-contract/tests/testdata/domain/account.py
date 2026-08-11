"""Canonical shared account domain factory for assurance contract fixtures."""

from __future__ import annotations

from typing import Any


async def make_account(**kwargs: Any) -> dict[str, Any]:
    """Create an account entity for layer tests (fixture stub)."""
    payload = {"name": "canonical-account", "status": "active"}
    payload.update(kwargs)
    return payload


async def cleanup_account(account_id: str) -> None:
    """Cleanup hook paired with make_account (fixture stub)."""
    _ = account_id
