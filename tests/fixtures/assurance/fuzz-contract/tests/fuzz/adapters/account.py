"""Fuzz account adapter stub for the canonical Fuzz fixture."""

from __future__ import annotations


async def seed_account(**kwargs):
    return {"id": "acc-1", **kwargs}


async def cleanup_account(account_id: str) -> None:
    _ = account_id
