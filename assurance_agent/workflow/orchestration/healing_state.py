"""Typed projection of the durable healing allocation ledger."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, Field

from assurance_agent.workflow.core.events import Ledger
from assurance_agent.workflow.healing.projection import project_healing_episode

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
    projection = project_healing_episode(change_dir)
    if projection.baseline is None:
        # No episode pinned → attempts stay 0, but the orchestrator may already
        # have recorded a terminal judgment (e.g. `not_needed` on the happy path)
        # via a heal_transition event. Honor the latest one so report/archive
        # routing sees the recorded decision instead of a stale `pending` that
        # would block the DAG forever.
        latest_transition = ledger.latest(type="heal_transition")
        if latest_transition is None:
            return HealingStateSnapshot()
        return HealingStateSnapshot(status=str(latest_transition["to"]))
    baseline_seq = projection.baseline.source_seq
    episode_id = projection.baseline.episode_id
    latest = projection.latest_allocation
    after_allocation = latest.source_seq if latest is not None else baseline_seq
    apply_records = [record for record in projection.records if record.source_seq > after_allocation]
    transitions = ledger.filter(type="heal_transition", after_seq=baseline_seq)
    stopped = bool(ledger.filter(type="human_decision", action="stop", after_seq=baseline_seq))
    terminal_transitions = [t for t in transitions if t.get("to") in _TERMINAL]
    if terminal_transitions:
        # Preserve the actual recorded verdict (resolved/exhausted/failed/...)
        # instead of collapsing every terminal outcome into "not_needed" —
        # report/archive gates and audits depend on the real value.
        status = str(terminal_transitions[-1]["to"])
    elif stopped:
        status = "not_needed"
    else:
        status = str(transitions[-1]["to"]) if transitions else "pending"
    return HealingStateSnapshot(
        status=status,
        attempts_used=projection.attempts_used,
        all_fixers_no_op=bool(apply_records)
        and all(not record.files_modified for record in apply_records),
        episode_id=episode_id,
        attempt_id=latest.attempt_id if latest is not None else None,
    )
