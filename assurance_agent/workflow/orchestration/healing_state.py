"""Typed projection of the durable healing allocation ledger."""
from __future__ import annotations

from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, Field

from assurance_agent.workflow.core.events import read_events

_TERMINAL = {"resolved", "not_needed", "skipped", "exhausted", "failed"}


def _event_seq(event: dict[str, object]) -> int:
    seq = event.get("seq")
    return seq if isinstance(seq, int) else 0


class HealingStateSnapshot(BaseModel):
    status: str = "pending"
    attempts_used: int = Field(default=0, ge=0)
    all_fixers_no_op: bool = False
    episode_id: str | None = None
    attempt_id: str | None = None


class HealingStateProvider(Protocol):
    def __call__(self, change_dir: Path) -> HealingStateSnapshot: ...


def derive_healing_state(change_dir: Path) -> HealingStateSnapshot:
    events = read_events(change_dir)
    baseline = next(
        (e for e in reversed(events) if e.get("type") == "healing_entry_baseline_pinned"),
        None,
    )
    if baseline is None:
        return HealingStateSnapshot()
    baseline_seq = _event_seq(baseline)
    ended = any(
        _event_seq(e) > baseline_seq
        and (
            (e.get("type") == "heal_transition" and e.get("to") in _TERMINAL)
            or (e.get("type") == "human_decision" and e.get("action") == "stop")
        )
        for e in events
    )
    if ended:
        return HealingStateSnapshot(status="not_needed")

    episode_id = str(baseline["episode_id"])
    allocations = [
        e for e in events
        if e.get("type") == "healing_attempt_allocated" and e.get("episode_id") == episode_id
    ]
    unique = {str(e["operation_id"]): e for e in allocations}
    latest = max(unique.values(), key=_event_seq, default=None)
    after_allocation = _event_seq(latest) if latest else baseline_seq
    apply_events = [
        e for e in events
        if e.get("type") == "heal_record_apply" and _event_seq(e) > after_allocation
    ]
    transitions = [
        e for e in events
        if e.get("type") == "heal_transition" and _event_seq(e) > baseline_seq
    ]
    status = str(transitions[-1]["to"]) if transitions else "pending"
    return HealingStateSnapshot(
        status=status,
        attempts_used=len(unique),
        all_fixers_no_op=bool(apply_events) and all(not e.get("files_modified") for e in apply_events),
        episode_id=episode_id,
        attempt_id=str(latest["attempt_id"]) if latest else None,
    )
