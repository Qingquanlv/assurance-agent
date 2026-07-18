"""Shared domain operations that own progression commits.

Driver and CLI adapters call these; they alone stage strict events and state
updates through ``workflow.core.progression.transaction``.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import uuid4

from assurance_agent.artifacts.models import WorkflowState
from assurance_agent.change_location import ChangeLocation
from assurance_agent.exceptions import AaError
from assurance_agent.workflow.core.audit_scope import is_audited_gate_read
from assurance_agent.workflow.core.events import event_seq
from assurance_agent.workflow.core.progression import ProgressionTxn, transaction
from assurance_agent.workflow.orchestration.audit_evidence import build_gate_verdict_event
from assurance_agent.workflow.orchestration.write_guards import (
    assert_gate_verdict_transition,
    assert_skill_attestation,
)
from assurance_agent.workflow.execution.tree_hash import sha256_file
from assurance_agent.workflow.execution.tree_hash import hash_test_tree
from assurance_agent.workflow.healing.override_policy import (
    DECISION_REL_PATH,
    TOKEN_REL_PATH,
    assert_test_changes_override_allowed,
    build_test_changes_override_token,
    load_test_changes_override_policy,
    token_json_bytes,
)
from assurance_agent.workflow.healing.safety import assert_test_tree_unchanged_or_healing
from assurance_agent.workflow.orchestration.decision_support import resolve_decision_support
from assurance_agent.workflow.orchestration.engine import compute_status
from assurance_agent.workflow.orchestration.gates import check_gate, resolve_change_path
from assurance_agent.workflow.orchestration.healing_episode import HealingAttemptIntent
from assurance_agent.workflow.orchestration.schema import (
    ORCHESTRATOR_INTERNAL,
    WorkflowSchema,
    load_workflow_schema,
)

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
    # State keys are restricted to [A-Za-z0-9_]; ordinary ids ("case-design")
    # are unaffected, fan-out child ids ("case-gen[menu]") collapse to a
    # deterministic underscore form ("case_gen_menu").
    return re.sub(r"[^A-Za-z0-9]+", "_", phase_id).strip("_")


def _with_phase(state: WorkflowState, phase_id: str, entry: dict) -> WorkflowState:
    data = state.model_dump(mode="python", exclude_none=True)
    phases = dict(data.get("phases") or {})
    phases[_phase_key(phase_id)] = entry
    data["phases"] = phases
    return WorkflowState.model_validate(data)


def _with_healing_status(
    state: WorkflowState,
    status: str,
    *,
    change_dir: Path | None = None,
) -> WorkflowState:
    """Write healing judgment and sync ledger-derived counters into workflow-state.

    ``aa state heal`` only commits the terminal status; without copying
    ``attempts_used`` from the event ledger, state stays at 0 and diverges
    from ``derive_healing_state`` (report/dashboard drift).
    """
    data = state.model_dump(mode="python", exclude_none=True)
    phases = dict(data.get("phases") or {})
    healing = dict(phases.get("healing") or {})
    healing["status"] = status
    if change_dir is not None:
        from assurance_agent.workflow.orchestration.healing_state import derive_healing_state

        snap = derive_healing_state(change_dir)
        healing["attempts_used"] = snap.attempts_used
        healing["all_fixers_no_op"] = snap.all_fixers_no_op
        if snap.episode_id is not None:
            healing["episode_id"] = snap.episode_id
        if snap.attempt_id is not None:
            healing["attempt_id"] = snap.attempt_id
    phases["healing"] = healing
    data["phases"] = phases
    return WorkflowState.model_validate(data)


def _with_gate(state: WorkflowState, name: str, value: object) -> WorkflowState:
    data = state.model_dump(mode="python", exclude_none=True)
    gates = dict(data.get("gates") or {})
    gates[name] = value
    data["gates"] = gates
    return WorkflowState.model_validate(data)


def _phase_entry_from_state(state: WorkflowState, phase_id: str) -> dict | None:
    phases = state.model_dump(mode="python", exclude_none=True).get("phases") or {}
    entry = phases.get(_phase_key(phase_id))
    return entry if isinstance(entry, dict) else None


def _load_yaml(path: Path) -> dict[str, object] | None:
    if not path.is_file():
        return None
    try:
        import yaml

        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def _load_json(path: Path) -> dict[str, object] | None:
    if not path.is_file():
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return doc if isinstance(doc, dict) else None


_EXEC_RESULT_PHASES = frozenset({"execution", "healing-rerun"})
_INSPECT_RESULT_PHASES = frozenset({"inspect", "healing-reinspect"})
_EXEC_STATUSES = frozenset({"PASS", "PASS_WITH_WARNINGS", "FAIL", "SKIPPED"})


def _enrich_phase_entry(
    loc: ChangeLocation,
    phase_id: str,
    entry: dict[str, object],
    state: WorkflowState,
) -> tuple[dict[str, object], WorkflowState]:
    """Stamp runbook-required fields from on-disk evidence (FALLBACK-RUNBOOK).

    - execution / healing-rerun: ``status = final_status``, ``batch_id`` from
      ``execution/execution-manifest.yaml`` (never invent PASS/FAIL).
    - inspect / healing-reinspect: ``inspect_mode`` (+ status partial when not
      primary) from ``inspect/failure-analysis.json``.
    - skill-registry-check: ``gates.healing_available`` from params /
      skill presence (true when healing is configured and skills are present).
    """
    change_dir = loc.path
    if phase_id in _EXEC_RESULT_PHASES:
        manifest = _load_yaml(change_dir / "execution" / "execution-manifest.yaml")
        if manifest is not None:
            final = manifest.get("final_status")
            if isinstance(final, str) and final in _EXEC_STATUSES:
                entry["status"] = final
            batch = manifest.get("batch_id")
            if isinstance(batch, str) and batch:
                entry["batch_id"] = batch
        state = _ensure_healing_available(loc, state)
        return entry, state

    if phase_id in _INSPECT_RESULT_PHASES:
        analysis = _load_json(change_dir / "inspect" / "failure-analysis.json")
        if analysis is not None:
            mode = analysis.get("inspect_mode")
            if isinstance(mode, str) and mode:
                entry["inspect_mode"] = mode
                if mode != "primary":
                    entry["status"] = "partial"
        state = _ensure_healing_available(loc, state)
        return entry, state

    if phase_id == "skill-registry-check":
        state = _ensure_healing_available(loc, state, force=True)
        return entry, state

    return entry, state


def _ensure_healing_available(
    loc: ChangeLocation,
    state: WorkflowState,
    *,
    force: bool = False,
) -> WorkflowState:
    """Stamp gates.healing_available from skills + params (runbook invariant)."""
    if not force and state.gates.healing_available is not None:
        return state
    params = state.params if isinstance(state.params, dict) else {}
    max_attempts = params.get("max_healing_attempts")
    try:
        attempts_ok = int(max_attempts) > 0 if max_attempts is not None else True
    except (TypeError, ValueError):
        attempts_ok = True
    skills_root = loc.project_root / "skills"
    required = ("aa-fix-proposal", "aa-api-codegen-fixer", "aa-e2e-codegen-fixer")
    skills_ok = all((skills_root / name / "SKILL.md").is_file() for name in required)
    return _with_gate(state, "healing_available", bool(attempts_ok and skills_ok))


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
    loc: ChangeLocation,
    schema: WorkflowSchema,
    phase_id: str,
    *,
    attempt_id: str | None = None,
    skill: str | None = None,
    skill_md_path: str | None = None,
) -> AppliedOutcome:
    change_dir = loc.path
    if not schema.has_phase(phase_id):
        raise AaError(f"unknown phase '{phase_id}'")
    missing = [
        rel for rel in (schema.phase_produces(phase_id) or []) if not resolve_change_path(loc, rel).exists()
    ]
    if missing:
        raise AaError(f"missing declared produces: {', '.join(missing)}")

    assert_skill_attestation(schema, phase_id, skill=skill)

    applied_status = "pass" if phase_id in ORCHESTRATOR_INTERNAL else "done"
    manual = attempt_id is None
    outcome_id = attempt_id or f"manual:{phase_id}:{uuid4()}"

    with transaction(change_dir) as txn:
        state = txn.read_state()
        outcomes = txn.ledger.filter(type="phase_outcome_committed", attempt_id=outcome_id)
        if outcomes:
            return _replay_or_reconcile_outcome(
                txn,
                loc,
                state,
                phase_id,
                outcome_id,
                applied_status,
                outcomes,
                skill,
                skill_md_path,
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
        entry, state = _enrich_phase_entry(loc, phase_id, entry, state)
        applied_status = str(entry.get("status") or applied_status)

        gate_report: dict[str, object] | None = None
        gate_name = schema.gate_for_phase(phase_id)
        if gate_name is not None:
            params = dict(state.params) if state.params else {}
            verdict = check_gate(schema, gate_name, loc, state, params)
            verdict_str = verdict.verdict.value if hasattr(verdict.verdict, "value") else str(verdict.verdict)
            assert_gate_verdict_transition(
                change_dir,
                schema,
                gate_id=verdict.gate,
                new_verdict=verdict_str,
                phase=phase_id,
            )
            gate_event = build_gate_verdict_event(
                loc,
                schema,
                phase=phase_id,
                gate=verdict.gate,
                verdict=verdict_str,
                matched_rule=verdict.matched_rule,
                reason=verdict.reason,
            )
            txn.append_strict(gate_event)
            gate_report = {
                "gate": verdict.gate,
                "verdict": verdict_str,
                "matched_rule": verdict.matched_rule,
                "reason": verdict.reason,
                "reads_sha256": gate_event.get("reads_sha256"),
            }

        txn.append_strict(
            {
                "source": "progression",
                "type": "phase_outcome_committed",
                "phase": phase_id,
                "attempt_id": outcome_id,
                "gate_report": gate_report,
            }
        )
        txn.set_state(_with_phase(state, phase_id, entry))
        return AppliedOutcome(
            phase_id=phase_id,
            attempt_id=outcome_id,
            applied_status=applied_status,
            disposition="committed",
        )


ARCHIVE_STATUSES = frozenset({"archived", "archived_with_warnings", "skipped"})


def commit_archive_outcome(
    loc: ChangeLocation,
    schema: WorkflowSchema,
    *,
    status: str,
    skill: str | None = "aa-archive",
    skill_md_path: str | None = None,
) -> AppliedOutcome:
    """Guarded commit of the out-of-band archive outcome.

    The archive phase runs post-terminal and out of band (``auto_archive`` is
    false in the loop; ``aa-archive`` writes ``qa/archive/<id>/`` after the
    driver already reached ``report``). Historically the skill hand-edited
    ``phases.archive.status`` straight into ``workflow-state.yaml``, which left
    ``_integrity.state_sha256`` stale and emitted no ledger event — the
    read-side auditor then flagged ``STATE-INTEGRITY-TAMPERED``.

    This routes that write through ``ProgressionTxn`` (same boundary as the
    driver commit path) so ``_integrity`` is re-hashed and a
    ``phase_outcome_committed`` event is recorded, and it enforces skill
    attestation. The ``archive-gate`` is a *driver-dispatch* precondition
    (``params.auto_archive``/``user_requested_archive``) and is intentionally
    not re-evaluated here: eligibility was already enforced by the loop/skill
    before the archive artifacts were written.
    """
    change_dir = loc.path
    if status not in ARCHIVE_STATUSES:
        raise AaError(f"invalid archive status {status!r}; expected one of {sorted(ARCHIVE_STATUSES)}")
    if not schema.has_phase("archive"):
        raise AaError("schema has no 'archive' phase")
    if status != "skipped":
        missing = [
            rel
            for rel in (schema.phase_produces("archive") or [])
            if not resolve_change_path(loc, rel).exists()
        ]
        if missing:
            raise AaError(f"missing declared produces: {', '.join(missing)}")

    assert_skill_attestation(schema, "archive", skill=skill)

    outcome_id = f"manual:archive:{uuid4()}"
    with transaction(change_dir) as txn:
        state = txn.read_state()
        outcomes = txn.ledger.filter(type="phase_outcome_committed", attempt_id=outcome_id)
        if outcomes:  # uuid collision is effectively impossible; replay defensively
            return AppliedOutcome(
                phase_id="archive",
                attempt_id=outcome_id,
                applied_status=status,
                disposition="replayed",
            )
        entry: dict[str, object] = {
            "status": status,
            "attempt_id": None,
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
                "phase": "archive",
                "attempt_id": outcome_id,
                "gate_report": None,
            }
        )
        txn.set_state(_with_phase(state, "archive", entry))
    return AppliedOutcome(
        phase_id="archive",
        attempt_id=outcome_id,
        applied_status=status,
        disposition="committed",
    )


def _replay_or_reconcile_outcome(
    txn: ProgressionTxn,
    loc: ChangeLocation,
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
        new_entry, state = _enrich_phase_entry(loc, phase_id, new_entry, state)
        applied_status = str(new_entry.get("status") or applied_status)
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
            txn.set_state(_with_healing_status(state, status, change_dir=change_dir))
            return HealTransition(
                from_status=str(latest.get("from") or "pending"),
                to_status=status,
                disposition="reconciled",
            )

        prior = ledger_to or state_status or "pending"
        txn.append_strict({"source": "status", "type": "heal_transition", "from": prior, "to": status})
        txn.set_state(_with_healing_status(state, status, change_dir=change_dir))
        return HealTransition(from_status=prior, to_status=status, disposition="committed")


def record_decision(
    loc: ChangeLocation,
    *,
    checkpoint: str,
    action: str,
    reason: str,
    who: str,
    evidence: str | None = None,
) -> None:
    project_root = loc.project_root
    change_dir = loc.path
    if action not in HUMAN_DECISION_ACTIONS:
        raise AaError(f"unsupported action '{action}'")
    if not reason.strip():
        raise AaError("decision reason is required")

    # Mirror of TS decide.ts ordering: resolveDecisionSupport validates the
    # (checkpoint, action) pair against the decision-support matrix BEFORE any
    # artifact binding, so unknown checkpoints and unsupported combinations
    # never produce an event.
    schema = load_workflow_schema(project_root)
    support = resolve_decision_support(schema, checkpoint, action)

    evidence_file: str | None = None
    evidence_sha256: str | None = None
    if evidence is not None:
        evidence_path = (project_root / evidence).resolve()
        try:
            evidence_path.relative_to(project_root.resolve())
        except ValueError as err:
            raise AaError("evidence must stay under project root") from err
        if not evidence_path.is_file():
            raise AaError(f"evidence not found: {evidence}")
        evidence_file = evidence_path.relative_to(project_root).as_posix()
        evidence_sha256 = hashlib.sha256(evidence_path.read_bytes()).hexdigest()

    override_token_bytes: bytes | None = None
    if support.consumer == "execution-test-changes":
        integrity = assert_test_tree_unchanged_or_healing(
            project_root,
            loc.change_id,
            allow_test_changes=True,
        )
        assert_test_changes_override_allowed(
            change_dir,
            integrity,
            load_test_changes_override_policy(project_root),
        )
        token = build_test_changes_override_token(
            change_dir,
            change_id=loc.change_id,
            reason=reason,
            tests_tree_sha256=hash_test_tree(project_root).aggregate,
        )
        override_token_bytes = token_json_bytes(token)
        evidence_file = DECISION_REL_PATH.as_posix()
        evidence_sha256 = hashlib.sha256(override_token_bytes).hexdigest()

    # Gate decisions auto-bind the current audited gate read as review evidence so
    # the engine's applyGateDecision can upgrade a needs_human_review verdict and
    # the read-side audit can later validate the anchor (mirror of TS
    # ``bindCurrentAuditedRead``).
    review_file: str | None = None
    review_sha256: str | None = None
    if checkpoint == "healing.safety" and action == "accept_risk":
        # Mirror of TS decide.ts ``requireChangeArtifact``: ``healing.safety`` is a
        # special checkpoint (neither a gate id nor a gated phase), so accept_risk
        # binds the fixer safety artifact directly — required, not best-effort.
        digest = sha256_file(change_dir / "healing" / "fixer-safety-check.json")
        if not digest:
            raise AaError("healing/fixer-safety-check.json is required for this decision")
        review_file = "healing/fixer-safety-check.json"
        review_sha256 = digest
    elif support.gate_id is not None and action != "stop":
        gate = schema.gates.get(support.gate_id)
        for read in gate.reads if gate is not None else []:
            if not is_audited_gate_read(read.path):
                continue
            digest = sha256_file(resolve_change_path(loc, read.path))
            if digest:
                review_file = read.path
                review_sha256 = digest
                break

    # Mirror of TS decide.ts: a non-stop gate decision whose gate declares audited
    # reads MUST bind one — without review_file/review_sha256 the engine can never
    # consume the decision, so fail closed instead of recording a dead event.
    if support.gate_id is not None and action != "stop":
        gate = schema.gates.get(support.gate_id)
        audited = (
            next(
                (r.path for r in gate.reads if is_audited_gate_read(r.path)),
                None,
            )
            if gate is not None
            else None
        )
        if audited is not None and review_file is None:
            raise AaError(f"Audited artifact {audited} is required for this decision")

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
            status = compute_status(schema, loc, state, state.params)
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

        if override_token_bytes is not None:
            txn.write_file(TOKEN_REL_PATH.as_posix(), override_token_bytes)
            txn.write_file(DECISION_REL_PATH.as_posix(), override_token_bytes)

        event: dict[str, object] = {
            "source": "decide",
            "type": "human_decision",
            "checkpoint": checkpoint,
            "action": action,
            "reason": reason,
            "who": who,
        }
        if evidence_file is not None:
            event["evidence_file"] = evidence_file
            event["evidence_sha256"] = evidence_sha256
        if review_file is not None:
            event["review_file"] = review_file
            event["review_sha256"] = review_sha256
        txn.append_strict(event)
        txn.set_state(WorkflowState.model_validate(data))
