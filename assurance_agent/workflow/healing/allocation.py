"""Healing attempt allocation ledger commits for graph v2."""

from __future__ import annotations

from pathlib import Path

from assurance_agent.workflow.core.events import (
    HealingAttemptAllocatedEvent,
    HealingEntryBaselinePinnedEvent,
    Ledger,
)
from assurance_agent.workflow.core.progression import transaction


def commit_healing_allocation_ledger(
    change_dir: Path,
    *,
    episode_id: str,
    attempt_id: str,
    attempt_number: int,
    operation_id: str,
    source_batch_id: str,
    baseline_sha256: str,
    entry_batch_id: str,
) -> bool:
    """Idempotently append baseline-pinned + attempt-allocated audit events.

    Returns True when a new allocation event was appended, False on replay.
    """
    ledger = Ledger(change_dir)
    if any(
        e.get("operation_id") == operation_id
        for e in ledger.filter(type="healing_attempt_allocated", episode_id=episode_id)
    ):
        return False

    with transaction(change_dir) as txn:
        baseline_exists = any(
            e.get("episode_id") == episode_id for e in txn.ledger.filter(type="healing_entry_baseline_pinned")
        )
        if not baseline_exists:
            txn.append_strict(
                HealingEntryBaselinePinnedEvent(
                    artifact_file="healing/entry-baseline.json",
                    artifact_sha256=baseline_sha256,
                    entry_batch_id=entry_batch_id,
                    episode_id=episode_id,
                )
            )
        txn.append_strict(
            HealingAttemptAllocatedEvent(
                episode_id=episode_id,
                attempt_id=attempt_id,
                attempt_number=attempt_number,
                operation_id=operation_id,
                source_batch_id=source_batch_id,
            )
        )
    return True


__all__ = ["commit_healing_allocation_ledger"]
