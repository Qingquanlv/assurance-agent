"""Write-side guards — refuse illegal commits at the progression boundary.

Layer 1 of the audit architecture: failures are loud ``AaError``s at commit time,
not silent ``stopped`` terminals discovered later by the read-side auditor.
"""

from __future__ import annotations

from pathlib import Path

from assurance_agent.exceptions import AaError
from assurance_agent.workflow.orchestration.audit import (
    SKILL_LOAD_EXEMPT_PHASES,
    check_verdict_migration,
    build_repair_map,
)
from assurance_agent.workflow.core.events import event_seq, read_events
from assurance_agent.workflow.orchestration.schema import WorkflowSchema


def assert_skill_attestation(
    schema: WorkflowSchema,
    phase_id: str,
    *,
    skill: str | None,
) -> None:
    """Refuse terminal commit when a skill-backed phase lacks skill attestation."""
    if phase_id in SKILL_LOAD_EXEMPT_PHASES:
        return
    phase = next((p for p in schema.phases if p.id == phase_id), None)
    if phase is None or phase.skill is None:
        return
    if skill is not None:
        return
    raise AaError(
        f"SKILL_LOAD_GATE_VIOLATION: phase {phase_id} cannot be committed without "
        f"skill attestation (expected skill={phase.skill!r})"
    )


def assert_gate_verdict_transition(
    change_dir: Path,
    schema: WorkflowSchema,
    *,
    gate_id: str,
    new_verdict: str,
    phase: str | None = None,
) -> None:
    """Refuse appending a gate_verdict that would create an illegal migration."""
    events = read_events(change_dir)
    prior = [e for e in events if e.get("type") == "gate_verdict" and e.get("gate") == gate_id]
    if not prior:
        return
    last = prior[-1]
    # Same-verdict re-checks also flow through the migration check: pass→pass
    # hash-drift rules still apply to the synthetic next event below.
    nxt: dict[str, object] = {
        "type": "gate_verdict",
        "gate": gate_id,
        "verdict": new_verdict,
        "phase": phase,
        "seq": event_seq(last) + 1,
    }
    # Streak start = earliest consecutive same-verdict as last.
    streak_start = len(prior) - 1
    while streak_start > 0 and prior[streak_start - 1].get("verdict") == last.get("verdict"):
        streak_start -= 1
    issue = check_verdict_migration(
        events + [nxt],
        schema,
        gate_id,
        last,
        nxt,
        build_repair_map(schema),
        event_seq(prior[streak_start]),
    )
    if issue is not None:
        raise AaError(issue.message)
