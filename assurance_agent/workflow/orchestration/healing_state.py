"""Typed projection of the durable healing allocation ledger."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, Field

from assurance_agent.workflow.core.events import Ledger, event_seq

_TERMINAL = {"resolved", "not_needed", "skipped", "exhausted", "failed"}


class HealingStateSnapshot(BaseModel):
    status: str = "pending"
    attempts_used: int = Field(default=0, ge=0)
    all_fixers_no_op: bool = False
    episode_id: str | None = None
    attempt_id: str | None = None


class HealingStateProvider(Protocol):
    def __call__(self, change_dir: Path) -> HealingStateSnapshot: ...


def derive_healing_state(change_dir: Path) -> HealingStateSnapshot:
    ledger = Ledger(change_dir)
    baseline = ledger.latest(type="healing_entry_baseline_pinned")
    if baseline is None:
        # No episode pinned → attempts stay 0, but the orchestrator may already
        # have recorded a terminal judgment (e.g. `not_needed` on the happy path)
        # via `aa state heal`, which appends a heal_transition event. Honor the
        # latest one so report/archive routing sees the recorded decision instead
        # of a stale `pending` that would block the DAG forever.
        latest_transition = ledger.latest(type="heal_transition")
        if latest_transition is None:
            return HealingStateSnapshot()
        return HealingStateSnapshot(status=str(latest_transition["to"]))
    baseline_seq = event_seq(baseline)
    ended = any(
        e.get("to") in _TERMINAL
        for e in ledger.filter(type="heal_transition", after_seq=baseline_seq)
    ) or bool(ledger.filter(type="human_decision", action="stop", after_seq=baseline_seq))
    if ended:
        return HealingStateSnapshot(status="not_needed")

    episode_id = str(baseline["episode_id"])
    allocations = ledger.filter(type="healing_attempt_allocated", episode_id=episode_id)
    unique = {str(e["operation_id"]): e for e in allocations}
    latest = max(unique.values(), key=event_seq, default=None)
    after_allocation = event_seq(latest) if latest else baseline_seq
    apply_events = ledger.filter(type="heal_record_apply", after_seq=after_allocation)
    transitions = ledger.filter(type="heal_transition", after_seq=baseline_seq)
    status = str(transitions[-1]["to"]) if transitions else "pending"
    return HealingStateSnapshot(
        status=status,
        attempts_used=len(unique),
        all_fixers_no_op=bool(apply_events) and all(not e.get("files_modified") for e in apply_events),
        episode_id=episode_id,
        attempt_id=str(latest["attempt_id"]) if latest else None,
    )
