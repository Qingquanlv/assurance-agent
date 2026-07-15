from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, Field, model_validator

from assurance_agent.artifacts.models import WorkflowState
from assurance_agent.workflow.core.events import read_events
from assurance_agent.workflow.orchestration.dsl import is_satisfied, parse_expression
from assurance_agent.workflow.orchestration.gates import build_evidence_scope, check_gate
from assurance_agent.workflow.orchestration.healing_state import HealingStateSnapshot
from assurance_agent.workflow.orchestration.schema import ReadEntry, WorkflowSchema


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


def _seq(event: dict[str, object]) -> int:
    seq = event.get("seq")
    return seq if isinstance(seq, int) else 0


def _latest(
    events: list[dict[str, object]],
    event_type: str,
    *,
    phase: str | None = None,
    after: int = 0,
):
    matches = [
        e
        for e in events
        if e.get("type") == event_type and (phase is None or e.get("phase") == phase) and _seq(e) > after
    ]
    return max(matches, key=_seq, default=None)


def _episode_floor(events: list[dict[str, object]], healing: HealingStateSnapshot) -> int:
    if healing.episode_id:
        baselines = [
            e
            for e in events
            if e.get("type") == "healing_entry_baseline_pinned" and e.get("episode_id") == healing.episode_id
        ]
        return _seq(max(baselines, key=_seq)) if baselines else 0
    terminal = [
        e
        for e in events
        if (e.get("type") == "heal_transition" and e.get("to") in {"resolved", "exhausted", "failed"})
        or (e.get("type") == "human_decision" and e.get("action") == "stop")
    ]
    return _seq(max(terminal, key=_seq)) if terminal else 0


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
    change_dir: Path,
    state: WorkflowState,
    params: dict,
    healing: HealingStateSnapshot,
) -> HealingEpisodeSnapshot:
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
    events = read_events(change_dir)
    episode_floor = _episode_floor(events, healing)
    allocations = [
        e
        for e in events
        if e.get("type") == "healing_attempt_allocated"
        and healing.episode_id is not None
        and e.get("episode_id") == healing.episode_id
    ]
    allocation = max(allocations, key=_seq, default=None)

    if allocation is None:
        gate_result = check_gate(schema, "healing-entry-gate", change_dir, state, merged)
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
            return HealingEpisodeSnapshot(state="inactive", stage=None)
        proposal = _latest(events, "phase_outcome_committed", phase="fix-proposal", after=episode_floor)
        if proposal is None:
            return _dispatch("fix-proposal", "proposal", 0)
        reads = [ReadEntry(path=p, alias=a) for a, p in schema.produces_alias_map().items()]
        scope = build_evidence_scope(schema, change_dir, state, merged, reads, hoist_primary=False)
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

    allocation_seq = _seq(allocation)
    attempt = healing.attempts_used
    proposal_doc = json.loads((change_dir / "healing/fix-proposal.json").read_text())
    targets = {
        item["target"]
        for item in proposal_doc.get("proposals", [])
        if item.get("eligible") is True and item.get("target") in {"api", "e2e"}
    }
    applied = {
        str(e.get("target"))
        for e in events
        if e.get("type") == "heal_record_apply"
        and _seq(e) > allocation_seq
        and e.get("source_batch_id") == allocation.get("source_batch_id")
    }
    missing = sorted(targets - applied)
    if missing:
        phase = "api-codegen-fix" if missing[0] == "api" else "e2e-codegen-fix"
        return _dispatch(phase, "apply", attempt)

    safety = check_gate(schema, "fixer-safety-gate", change_dir, state, merged).verdict.value
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

    rerun = _latest(events, "phase_outcome_committed", phase="healing-rerun", after=allocation_seq)
    if rerun is None:
        return _dispatch("healing-rerun", "rerun", attempt)
    reinspect = _latest(events, "phase_outcome_committed", phase="healing-reinspect", after=_seq(rerun))
    if reinspect is None:
        return _dispatch("healing-reinspect", "reinspect", attempt)

    loop_verdict = check_gate(schema, loop.exit_gate, change_dir, state, merged).verdict.value
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
    proposal = _latest(events, "phase_outcome_committed", phase="fix-proposal", after=_seq(reinspect))
    if proposal is None:
        return _dispatch("fix-proposal", "proposal", attempt)
    return _allocate_snapshot(
        change_dir,
        state,
        healing,
        current_attempt=attempt,
        next_attempt=attempt + 1,
    )
