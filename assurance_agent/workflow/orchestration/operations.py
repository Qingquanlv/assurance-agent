"""Shared domain operations that own progression commits.

Driver and CLI adapters call these; they alone stage strict events and state
updates through ``workflow.core.progression.transaction``.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import uuid4

from assurance_agent.artifacts.models import WorkflowState
from assurance_agent.exceptions import AaError
from assurance_agent.workflow.core.events import event_seq
from assurance_agent.workflow.core.progression import ProgressionTxn, transaction
from assurance_agent.workflow.orchestration.engine import compute_status
from assurance_agent.workflow.orchestration.gates import resolve_change_path
from assurance_agent.workflow.orchestration.healing_episode import HealingAttemptIntent
from assurance_agent.workflow.orchestration.schema import ORCHESTRATOR_INTERNAL, WorkflowSchema

HEAL_STATUSES = frozenset({"resolved", "exhausted", "not_needed", "failed", "skipped"})
HUMAN_DECISION_ACTIONS = frozenset(
    {"fix_and_proceed", "accept_risk", "stop", "allow_test_changes", "skip_branch"}
)
BASELINE_REL = "healing/entry-baseline.json"


class StaleDispatchError(AaError):
    """First outcome commit rejected because the signed state_guard drifted."""


@dataclass(frozen=True)
class DispatchReceipt:
    phase_id: str
    attempt_id: str
    state_guard: str
    disposition: Literal["committed", "replayed"]


@dataclass(frozen=True)
class AppliedOutcome:
    phase_id: str
    attempt_id: str
    applied_status: str
    disposition: Literal["committed", "replayed", "reconciled", "superseded"]


@dataclass(frozen=True)
class HealingAllocationResult:
    operation_id: str
    attempt_id: str
    disposition: Literal["committed", "replayed", "reconciled"]


@dataclass(frozen=True)
class HealTransition:
    from_status: str
    to_status: str
    disposition: Literal["committed", "replayed", "reconciled"]


def _phase_key(phase_id: str) -> str:
    return phase_id.replace("-", "_")


def _with_phase(state: WorkflowState, phase_id: str, entry: dict) -> WorkflowState:
    data = state.model_dump(mode="python", exclude_none=True)
    phases = dict(data.get("phases") or {})
    phases[_phase_key(phase_id)] = entry
    data["phases"] = phases
    return WorkflowState.model_validate(data)


def _with_healing_status(state: WorkflowState, status: str) -> WorkflowState:
    data = state.model_dump(mode="python", exclude_none=True)
    phases = dict(data.get("phases") or {})
    healing = dict(phases.get("healing") or {})
    healing["status"] = status
    phases["healing"] = healing
    data["phases"] = phases
    return WorkflowState.model_validate(data)


def _phase_entry_from_state(state: WorkflowState, phase_id: str) -> dict | None:
    phases = state.model_dump(mode="python", exclude_none=True).get("phases") or {}
    entry = phases.get(_phase_key(phase_id))
    return entry if isinstance(entry, dict) else None


def record_dispatch(
    change_dir: Path,
    *,
    phase_id: str,
    kind: Literal["dispatch_phase", "heal"],
    attempt_id: str,
    target: Literal["api", "e2e"] | None = None,
    dispatched_at: int | None = None,
) -> DispatchReceipt:
    with transaction(change_dir) as txn:
        guard = txn.current_state_guard()
        prior = txn.ledger.latest(type="dispatch_signed", attempt_id=attempt_id)
        if prior is not None:
            same = (
                prior.get("phase") == phase_id
                and prior.get("kind") == kind
                and prior.get("target") == target
                and prior.get("state_guard") == guard
            )
            if not same:
                raise AaError(f"dispatch attempt_id conflict: {attempt_id}")
            return DispatchReceipt(
                phase_id=phase_id,
                attempt_id=attempt_id,
                state_guard=str(prior.get("state_guard") or guard),
                disposition="replayed",
            )
        at = dispatched_at if dispatched_at is not None else int(time.time() * 1000)
        txn.append_strict(
            {
                "source": "progression",
                "type": "dispatch_signed",
                "phase": phase_id,
                "kind": kind,
                "target": target,
                "attempt_id": attempt_id,
                "state_guard": guard,
                "dispatched_at": at,
            }
        )
        return DispatchReceipt(
            phase_id=phase_id,
            attempt_id=attempt_id,
            state_guard=guard,
            disposition="committed",
        )


def apply_phase_outcome(
    project_root: Path,
    change_dir: Path,
    schema: WorkflowSchema,
    phase_id: str,
    *,
    attempt_id: str | None = None,
    skill: str | None = None,
    skill_md_path: str | None = None,
) -> AppliedOutcome:
    if not schema.has_phase(phase_id):
        raise AaError(f"unknown phase '{phase_id}'")
    missing = [
        rel
        for rel in (schema.phase_produces(phase_id) or [])
        if not resolve_change_path(change_dir, rel).exists()
    ]
    if missing:
        raise AaError(f"missing declared produces: {', '.join(missing)}")

    applied_status = "pass" if phase_id in ORCHESTRATOR_INTERNAL else "done"
    manual = attempt_id is None
    outcome_id = attempt_id or f"manual:{phase_id}:{uuid4()}"

    with transaction(change_dir) as txn:
        state = txn.read_state()
        outcomes = txn.ledger.filter(type="phase_outcome_committed", attempt_id=outcome_id)
        if outcomes:
            return _replay_or_reconcile_outcome(
                txn, state, phase_id, outcome_id, applied_status, outcomes, skill, skill_md_path
            )

        if not manual:
            signed = txn.ledger.latest(type="dispatch_signed", attempt_id=outcome_id, phase=phase_id)
            if signed is None:
                raise AaError(f"no matching dispatch_signed for attempt_id={outcome_id}")
            current_guard = txn.current_state_guard()
            if str(signed.get("state_guard") or "") != current_guard:
                raise StaleDispatchError(
                    f"state_guard drifted since dispatch of {outcome_id}: "
                    f"signed={signed.get('state_guard')!r} current={current_guard!r}"
                )

        entry: dict[str, object] = {
            "status": applied_status,
            "attempt_id": outcome_id,
            "skill_loaded": skill is not None,
            "skill_md_path": skill_md_path,
            "skill_loaded_at": datetime.now(timezone.utc).isoformat(),
        }
        if skill is not None:
            entry["skill"] = skill
        txn.append_strict(
            {
                "source": "progression",
                "type": "phase_outcome_committed",
                "phase": phase_id,
                "attempt_id": outcome_id,
                "gate_report": None,
            }
        )
        txn.set_state(_with_phase(state, phase_id, entry))
        return AppliedOutcome(
            phase_id=phase_id,
            attempt_id=outcome_id,
            applied_status=applied_status,
            disposition="committed",
        )


def _replay_or_reconcile_outcome(
    txn: ProgressionTxn,
    state: WorkflowState,
    phase_id: str,
    outcome_id: str,
    applied_status: str,
    outcomes: list[dict],
    skill: str | None,
    skill_md_path: str | None,
) -> AppliedOutcome:
    prior = txn.ledger.latest(type="phase_outcome_committed", attempt_id=outcome_id)
    assert prior is not None
    if prior.get("phase") != phase_id:
        raise AaError(f"attempt_id {outcome_id} already committed for phase {prior.get('phase')}")

    entry = _phase_entry_from_state(state, phase_id)
    marked = entry.get("attempt_id") if entry else None
    latest = txn.ledger.latest(type="phase_outcome_committed", phase=phase_id)
    latest_id = latest.get("attempt_id") if latest else None

    if marked == outcome_id:
        return AppliedOutcome(
            phase_id=phase_id,
            attempt_id=outcome_id,
            applied_status=str(entry.get("status") or applied_status) if entry else applied_status,
            disposition="replayed",
        )
    if marked and marked != outcome_id:
        # Marker points at a later/other outcome for this phase — keep newer state.
        if latest_id == marked:
            return AppliedOutcome(
                phase_id=phase_id,
                attempt_id=outcome_id,
                applied_status=str(entry.get("status") or applied_status) if entry else applied_status,
                disposition="superseded",
            )
        raise AaError(f"phase {phase_id} marker attempt_id={marked} does not match ledger")

    # Event present, marker missing, and this outcome is still the latest for the phase.
    if latest_id == outcome_id:
        new_entry: dict[str, object] = {
            "status": applied_status,
            "attempt_id": outcome_id,
            "skill_loaded": skill is not None,
            "skill_md_path": skill_md_path,
            "skill_loaded_at": datetime.now(timezone.utc).isoformat(),
        }
        if skill is not None:
            new_entry["skill"] = skill
        txn.set_state(_with_phase(state, phase_id, new_entry))
        return AppliedOutcome(
            phase_id=phase_id,
            attempt_id=outcome_id,
            applied_status=applied_status,
            disposition="reconciled",
        )
    raise AaError(f"cannot reconcile outcome {outcome_id} for phase {phase_id}")


def allocate_healing_attempt(
    change_dir: Path,
    allocation: HealingAttemptIntent,
) -> HealingAllocationResult:
    baseline_payload = {
        "schema_version": "1.0",
        "episode_id": allocation.episode_id,
        "entry_batch_id": allocation.source_batch_id,
    }
    baseline_data = (json.dumps(baseline_payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    baseline_sha = hashlib.sha256(baseline_data).hexdigest()

    with transaction(change_dir) as txn:
        prior = txn.ledger.latest(type="healing_attempt_allocated", operation_id=allocation.operation_id)
        if prior is not None:
            raw_num = prior.get("attempt_number")
            prior_num = raw_num if isinstance(raw_num, int) else None
            if (
                prior.get("episode_id") != allocation.episode_id
                or prior.get("attempt_id") != allocation.attempt_id
                or prior_num != allocation.attempt_number
                or prior.get("source_batch_id") != allocation.source_batch_id
            ):
                raise AaError(f"operation_id conflict: {allocation.operation_id}")
            return HealingAllocationResult(
                operation_id=allocation.operation_id,
                attempt_id=allocation.attempt_id,
                disposition="replayed",
            )

        baseline_path = change_dir / BASELINE_REL
        baseline_exists = baseline_path.is_file()
        pinned = txn.ledger.filter(type="healing_entry_baseline_pinned", episode_id=allocation.episode_id)

        if allocation.pin_entry_baseline:
            if baseline_exists:
                existing = baseline_path.read_bytes()
                if hashlib.sha256(existing).hexdigest() != baseline_sha:
                    raise AaError("healing entry-baseline.json hash mismatch during reconcile")
            else:
                txn.write_file(BASELINE_REL, baseline_data)
            if not pinned:
                txn.append_strict(
                    {
                        "source": "heal",
                        "type": "healing_entry_baseline_pinned",
                        "artifact_file": "healing/entry-baseline.json",
                        "artifact_sha256": baseline_sha,
                        "entry_batch_id": allocation.source_batch_id,
                        "episode_id": allocation.episode_id,
                    }
                )
            elif max(pinned, key=event_seq).get("artifact_sha256") != baseline_sha:
                raise AaError("healing baseline pin event hash mismatch")

        txn.append_strict(
            {
                "source": "progression",
                "type": "healing_attempt_allocated",
                "episode_id": allocation.episode_id,
                "attempt_id": allocation.attempt_id,
                "attempt_number": allocation.attempt_number,
                "operation_id": allocation.operation_id,
                "source_batch_id": allocation.source_batch_id,
            }
        )
        disposition: Literal["committed", "reconciled"] = (
            "reconciled" if (baseline_exists or pinned) else "committed"
        )
        return HealingAllocationResult(
            operation_id=allocation.operation_id,
            attempt_id=allocation.attempt_id,
            disposition=disposition,
        )


def record_heal_transition(change_dir: Path, status: str) -> HealTransition:
    if status not in HEAL_STATUSES:
        allowed = ", ".join(sorted(HEAL_STATUSES))
        raise AaError(f'unsupported healing status "{status}". Expected one of: {allowed}')

    with transaction(change_dir) as txn:
        state = txn.read_state()
        latest = txn.ledger.latest(type="heal_transition")
        ledger_to = str(latest["to"]) if latest and latest.get("to") is not None else None
        state_status = state.phases.healing.status

        if ledger_to == status and state_status == status:
            assert latest is not None
            return HealTransition(
                from_status=str(latest.get("from") or "pending"),
                to_status=status,
                disposition="replayed",
            )

        if ledger_to == status and state_status != status:
            assert latest is not None
            txn.set_state(_with_healing_status(state, status))
            return HealTransition(
                from_status=str(latest.get("from") or "pending"),
                to_status=status,
                disposition="reconciled",
            )

        prior = ledger_to or state_status or "pending"
        txn.append_strict({"source": "status", "type": "heal_transition", "from": prior, "to": status})
        txn.set_state(_with_healing_status(state, status))
        return HealTransition(from_status=prior, to_status=status, disposition="committed")


def record_decision(
    project_root: Path,
    change_dir: Path,
    *,
    checkpoint: str,
    action: str,
    reason: str,
    who: str,
    evidence: str | None = None,
) -> None:
    if action not in HUMAN_DECISION_ACTIONS:
        raise AaError(f"unsupported action '{action}'")
    if not reason.strip():
        raise AaError("decision reason is required")

    review_file: str | None = None
    review_sha256: str | None = None
    if evidence is not None:
        evidence_path = (project_root / evidence).resolve()
        try:
            evidence_path.relative_to(project_root.resolve())
        except ValueError as err:
            raise AaError("evidence must stay under project root") from err
        if not evidence_path.is_file():
            raise AaError(f"evidence not found: {evidence}")
        review_file = evidence_path.relative_to(project_root).as_posix()
        review_sha256 = hashlib.sha256(evidence_path.read_bytes()).hexdigest()

    with transaction(change_dir) as txn:
        state = txn.read_state()
        data = state.model_dump(mode="python", exclude_none=True)
        # Rebuild decisions list from ledger where possible (append-only audit).
        ledger_decisions = [
            {
                "checkpoint": e.get("checkpoint"),
                "action": e.get("action"),
                "reason": e.get("reason"),
                "who": e.get("who"),
            }
            for e in txn.ledger.filter(type="human_decision")
        ]
        decisions: list[dict[str, object]] = (
            list(ledger_decisions)
            if ledger_decisions
            else [d for d in (data.get("decisions") or []) if isinstance(d, dict)]
        )

        stop_snapshot: dict | None = None
        if action == "stop":
            from assurance_agent.workflow.orchestration.schema import load_workflow_schema

            loaded = load_workflow_schema(project_root)
            status = compute_status(loaded, change_dir, state, state.params)
            if status.terminal is not None:
                raise AaError(f"workflow already terminal ({status.terminal.kind})")
            stop_snapshot = {
                "next": [d.phase_id for d in status.next_dispatch],
                "phases": {p.id: p.status for p in status.phases},
            }

        record: dict[str, object] = {
            "checkpoint": checkpoint,
            "action": action,
            "reason": reason,
            "who": who,
            "at": datetime.now(timezone.utc).isoformat(),
        }
        if stop_snapshot is not None:
            record["state_at_stop"] = stop_snapshot
        decisions.append(record)
        data["decisions"] = decisions
        if action == "stop":
            data["terminal"] = {"kind": "stopped", "reason": reason}

        event: dict[str, object] = {
            "source": "decide",
            "type": "human_decision",
            "checkpoint": checkpoint,
            "action": action,
            "reason": reason,
            "who": who,
        }
        if review_file is not None:
            event["review_file"] = review_file
            event["review_sha256"] = review_sha256
        txn.append_strict(event)
        txn.set_state(WorkflowState.model_validate(data))
