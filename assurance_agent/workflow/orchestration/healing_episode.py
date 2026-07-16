from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, Field, model_validator

from assurance_agent.artifacts.models import WorkflowState
from assurance_agent.change_location import ChangeLocation
from assurance_agent.workflow.core.events import Ledger, event_seq
from assurance_agent.workflow.orchestration.dsl import is_satisfied, parse_expression
from assurance_agent.workflow.orchestration.gates import build_evidence_scope, check_gate
from assurance_agent.workflow.orchestration.healing_state import HealingStateSnapshot
from assurance_agent.workflow.orchestration.schema import ReadEntry, WorkflowSchema

# Recorded healing judgments that satisfy report/archive `ready_when` routing.
_HEALING_TERMINAL = {"resolved", "not_needed", "skipped", "exhausted", "failed"}


class HealingAttemptIntent(BaseModel):
    episode_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    attempt_number: int = Field(ge=1)
    operation_id: str = Field(min_length=1)
    source_batch_id: str = Field(min_length=1)
    pin_entry_baseline: bool


class HealingEpisodeAction(BaseModel):
    kind: Literal["dispatch_phase", "allocate_attempt", "await_human", "complete"]
    phase: str | None = None
    allocation: HealingAttemptIntent | None = None
    outcome: str | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> Self:
        valid = (
            (
                self.kind == "dispatch_phase"
                and self.phase is not None
                and self.allocation is None
                and self.outcome is None
            )
            or (
                self.kind == "allocate_attempt"
                and self.phase is None
                and self.allocation is not None
                and self.outcome is None
            )
            or (
                self.kind == "await_human"
                and self.phase is None
                and self.allocation is None
                and self.outcome is None
            )
            or (
                self.kind == "complete"
                and self.phase is None
                and self.allocation is None
                and self.outcome is not None
            )
        )
        if not valid:
            raise ValueError(f"invalid healing action shape for kind={self.kind}")
        return self


class HealingEpisodeSnapshot(BaseModel):
    state: Literal["inactive", "active", "awaiting_human", "terminal"]
    stage: Literal["entry", "proposal", "allocate", "apply", "safety", "rerun", "reinspect", "decide"] | None
    attempt_number: int = 0
    next_actions: list[HealingEpisodeAction] = Field(default_factory=list)
    terminal_kind: Literal["stopped"] | None = None
    reason: str | None = None


def _episode_floor(ledger: Ledger, healing: HealingStateSnapshot) -> int:
    if healing.episode_id:
        baseline = ledger.latest(type="healing_entry_baseline_pinned", episode_id=healing.episode_id)
        return event_seq(baseline) if baseline else 0
    transitions = [
        e for e in ledger.filter(type="heal_transition") if e.get("to") in {"resolved", "exhausted", "failed"}
    ]
    stops = ledger.filter(type="human_decision", action="stop")
    terminal = transitions + stops
    return event_seq(max(terminal, key=event_seq)) if terminal else 0


def _dispatch(
    phase: str,
    stage: Literal["entry", "proposal", "allocate", "apply", "safety", "rerun", "reinspect", "decide"],
    attempt: int,
) -> HealingEpisodeSnapshot:
    return HealingEpisodeSnapshot(
        state="active",
        stage=stage,
        attempt_number=attempt,
        next_actions=[HealingEpisodeAction(kind="dispatch_phase", phase=phase)],
    )


def _allocation_intent(
    change_dir: Path,
    state: WorkflowState,
    healing: HealingStateSnapshot,
    attempt_number: int,
) -> HealingAttemptIntent | None:
    proposal = (change_dir / "healing/fix-proposal.json").read_bytes()
    batch = state.phases.execution.batch_id if state.phases.execution else ""
    if not batch:
        return None
    proposal_sha = hashlib.sha256(proposal).hexdigest()
    episode_id = (
        healing.episode_id or hashlib.sha256(f"{change_dir.name}:{batch}:{proposal_sha}".encode()).hexdigest()
    )
    operation_id = hashlib.sha256(f"{batch}:{proposal_sha}:{attempt_number}".encode()).hexdigest()
    return HealingAttemptIntent(
        episode_id=episode_id,
        attempt_id=f"ha-{episode_id[:12]}-{attempt_number}",
        attempt_number=attempt_number,
        operation_id=operation_id,
        source_batch_id=batch,
        pin_entry_baseline=healing.episode_id is None,
    )


def _allocate_snapshot(
    change_dir: Path,
    state: WorkflowState,
    healing: HealingStateSnapshot,
    *,
    current_attempt: int,
    next_attempt: int,
) -> HealingEpisodeSnapshot:
    intent = _allocation_intent(change_dir, state, healing, next_attempt)
    if intent is None:
        return HealingEpisodeSnapshot(
            state="terminal",
            stage="allocate",
            attempt_number=current_attempt,
            terminal_kind="stopped",
            reason="missing execution source_batch_id",
        )
    return HealingEpisodeSnapshot(
        state="active",
        stage="allocate",
        attempt_number=current_attempt,
        next_actions=[HealingEpisodeAction(kind="allocate_attempt", allocation=intent)],
    )


def project_healing_episode(
    schema: WorkflowSchema,
    loc: ChangeLocation,
    state: WorkflowState,
    params: dict,
    healing: HealingStateSnapshot,
) -> HealingEpisodeSnapshot:
    change_dir = loc.path
    loop = schema.loops.get("healing")
    if loop is None:
        return HealingEpisodeSnapshot(state="inactive", stage=None)
    merged = {**schema.default_param_values(), **params}
    data = state.model_dump(mode="python", exclude_none=True)
    phases = dict(data.get("phases") or {})
    phases["healing"] = {
        **dict(phases.get("healing") or {}),
        **healing.model_dump(mode="python", exclude_none=True),
    }
    data["phases"] = phases
    state = WorkflowState.model_validate(data)
    ledger = Ledger(change_dir)
    episode_floor = _episode_floor(ledger, healing)
    allocation = (
        ledger.latest(type="healing_attempt_allocated", episode_id=healing.episode_id)
        if healing.episode_id is not None
        else None
    )

    if allocation is None:
        gate_result = check_gate(schema, "healing-entry-gate", loc, state, merged)
        entry = gate_result.verdict.value
        if entry != "enter":
            if (
                entry == "stop"
                and gate_result.matched_rule is not None
                and gate_result.matched_rule.startswith("stop_when:")
            ):
                return HealingEpisodeSnapshot(
                    state="terminal",
                    stage="entry",
                    terminal_kind="stopped",
                    reason="healing-entry-gate=stop",
                )
            if (
                entry == "skip"
                and gate_result.matched_rule is not None
                and gate_result.matched_rule.startswith("skip_when:")
                and healing.status not in _HEALING_TERMINAL
            ):
                # Healing is definitively not required (execution passed, or no
                # eligible failures). Emit a single `complete(not_needed)` so the
                # driver records the decision (`aa state heal`) and report's
                # ready_when unblocks. Idempotent: once recorded, derive_healing_state
                # reports `not_needed` and this branch is skipped.
                return HealingEpisodeSnapshot(
                    state="terminal",
                    stage="entry",
                    next_actions=[HealingEpisodeAction(kind="complete", outcome="not_needed")],
                )
            return HealingEpisodeSnapshot(state="inactive", stage=None)
        proposal = ledger.latest(
            type="phase_outcome_committed", phase="fix-proposal", after_seq=episode_floor
        )
        if proposal is None:
            return _dispatch("fix-proposal", "proposal", 0)
        reads = [ReadEntry(path=p, alias=a) for a, p in schema.produces_alias_map().items()]
        scope = build_evidence_scope(schema, loc, state, merged, reads, hoist_primary=False)
        if not is_satisfied(parse_expression(loop.allocate_on), scope):
            return HealingEpisodeSnapshot(
                state="terminal",
                stage="allocate",
                terminal_kind="stopped",
                reason="healing allocate_on false after committed proposal",
            )
        return _allocate_snapshot(
            change_dir,
            state,
            healing,
            current_attempt=0,
            next_attempt=1,
        )

    allocation_seq = event_seq(allocation)
    attempt = healing.attempts_used
    proposal_doc = json.loads((change_dir / "healing/fix-proposal.json").read_text())
    targets = {
        item["target"]
        for item in proposal_doc.get("proposals", [])
        if item.get("eligible") is True and item.get("target") in {"api", "e2e"}
    }
    applied = {
        str(e.get("target"))
        for e in ledger.filter(
            type="heal_record_apply",
            after_seq=allocation_seq,
            source_batch_id=allocation.get("source_batch_id"),
        )
    }
    missing = sorted(targets - applied)
    if missing:
        phase = "api-codegen-fix" if missing[0] == "api" else "e2e-codegen-fix"
        return _dispatch(phase, "apply", attempt)

    safety = check_gate(schema, "fixer-safety-gate", loc, state, merged).verdict.value
    if safety == "needs_human_review":
        return HealingEpisodeSnapshot(
            state="awaiting_human",
            stage="safety",
            attempt_number=attempt,
            next_actions=[HealingEpisodeAction(kind="await_human")],
        )
    if safety != "pass":
        return HealingEpisodeSnapshot(
            state="terminal",
            stage="safety",
            attempt_number=attempt,
            terminal_kind="stopped",
            reason=f"fixer-safety-gate={safety}",
        )

    rerun = ledger.latest(type="phase_outcome_committed", phase="healing-rerun", after_seq=allocation_seq)
    if rerun is None:
        return _dispatch("healing-rerun", "rerun", attempt)
    reinspect = ledger.latest(
        type="phase_outcome_committed", phase="healing-reinspect", after_seq=event_seq(rerun)
    )
    if reinspect is None:
        return _dispatch("healing-reinspect", "reinspect", attempt)

    loop_verdict = check_gate(schema, loop.exit_gate, loc, state, merged).verdict.value
    if loop_verdict == "exit":
        return HealingEpisodeSnapshot(
            state="terminal",
            stage="decide",
            attempt_number=attempt,
            next_actions=[HealingEpisodeAction(kind="complete", outcome="resolved")],
        )
    if loop_verdict == "stop":
        maximum = int(merged[loop.max_param])
        return HealingEpisodeSnapshot(
            state="terminal",
            stage="decide",
            attempt_number=attempt,
            terminal_kind="stopped",
            reason=f"healing attempts exhausted: {attempt}/{maximum}",
        )
    proposal = ledger.latest(
        type="phase_outcome_committed", phase="fix-proposal", after_seq=event_seq(reinspect)
    )
    if proposal is None:
        return _dispatch("fix-proposal", "proposal", attempt)
    return _allocate_snapshot(
        change_dir,
        state,
        healing,
        current_attempt=attempt,
        next_attempt=attempt + 1,
    )
