"""Performance account seed adapter stub for the canonical Performance fixture."""

from __future__ import annotations


async def seed_accounts(batch_size: int = 50, **kwargs):
    return [{"id": f"acc-{i}"} for i in range(batch_size)]


async def cleanup_accounts(account_ids: list[str]) -> None:
    _ = account_ids
