"""Read-side status audits — pure projection over the events ledger.

Mirror of TS ``src/workflow/core/audit.ts``, plus folding of workflow-state
``_integrity`` checks into AuditIssues (instead of hard-crashing ``aa status``).

No writes. Same inputs always yield the same issues.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from assurance_agent.change_location import ChangeLocation
from assurance_agent.workflow.core.audit_scope import is_audited_gate_read
from assurance_agent.workflow.core.events import event_seq, read_events
from assurance_agent.workflow.core.state import verify_state_integrity
from assurance_agent.workflow.execution.tree_hash import sha256_file
from assurance_agent.workflow.orchestration.engine import PhaseView, Terminal, WorkflowStatus
from assurance_agent.workflow.orchestration.gates import resolve_change_path
from assurance_agent.workflow.orchestration.schema import WorkflowSchema

SKILL_LOAD_EXEMPT_PHASES = frozenset(
    {
        "skill_registry_check",
        "skill-registry-check",
        "layers",
        "execution",
        "healing-rerun",
        "healing_rerun",
    }
)

TERMINAL_SKILL_STATUSES = frozenset(
    {
        "done",
        "pass",
        "needs_fix",
        "needs_human_review",
        "reject",
        "failed",
        "partial",
        "unavailable",
        "stopped",
        "proposal_created",
        "applied",
        "resolved",
        "exhausted",
    }
)

SETTLED_GATE_VERDICTS = frozenset({"pass", "reject", "needs_human_review", "stop"})


@dataclass(frozen=True)
class AuditIssue:
    code: str
    message: str
    phase: str | None = None


@dataclass
class AuditResult:
    issues: list[AuditIssue] = field(default_factory=list)


def run_status_audits(
    loc: ChangeLocation,
    report: WorkflowStatus,
    schema: WorkflowSchema,
) -> AuditResult:
    issues: list[AuditIssue] = []
    issues.extend(_audit_state_integrity(loc.path))
    issues.extend(_audit_gate_tampering(loc, report, schema))
    issues.extend(_audit_skill_load_gate(loc.path))
    issues.extend(_audit_verdict_transitions(loc, schema))
    issues.extend(_audit_reclassification_events(loc.path))
    return AuditResult(issues=issues)


def apply_audits_to_report(report: WorkflowStatus, audit: AuditResult) -> WorkflowStatus:
    if not audit.issues:
        return report

    phases = list(report.phases)
    terminal = report.terminal

    for issue in audit.issues:
        if issue.code == "ARTIFACT-TAMPERED" and issue.phase:
            phases = [
                PhaseView(
                    id=p.id,
                    status="tampered",
                    gate=p.gate,
                    gate_verdict=p.gate_verdict,
                    produces_present=p.produces_present,
                )
                if p.id == issue.phase
                else p
                for p in phases
            ]
        terminal = Terminal(
            kind="stopped",
            reason=issue.message,
            phase=issue.phase or (terminal.phase if terminal else None),
        )

    return WorkflowStatus(
        phases=phases,
        next_dispatch=[],  # audits stop further dispatch
        terminal=terminal,
        healing_episode=report.healing_episode,
    )


def _audit_state_integrity(change_dir: Path) -> list[AuditIssue]:
    message = verify_state_integrity(change_dir)
    if message is None:
        return []
    return [
        AuditIssue(
            code="STATE-INTEGRITY-TAMPERED",
            message=f"STATE-INTEGRITY-TAMPERED: {message}",
        )
    ]


def _load_phases_doc(change_dir: Path) -> dict[str, Any] | None:
    path = change_dir / "workflow-state.yaml"
    if not path.exists():
        return None
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(doc, dict) or not isinstance(doc.get("phases"), dict):
        return None
    return doc["phases"]


def _audit_skill_load_gate(change_dir: Path) -> list[AuditIssue]:
    phases = _load_phases_doc(change_dir)
    if phases is None:
        return []
    issues: list[AuditIssue] = []
    for phase, raw in phases.items():
        if not isinstance(raw, dict):
            continue
        if not _phase_requires_skill_load(str(phase), raw):
            continue
        if raw.get("skill_loaded") is True:
            continue
        issues.append(
            AuditIssue(
                code="SKILL_LOAD_GATE_VIOLATION",
                phase=str(phase),
                message=(
                    f"SKILL_LOAD_GATE_VIOLATION: phase {phase} cannot be terminal because "
                    f"skill_loaded is {raw.get('skill_loaded')!r}; read the phase SKILL.md and "
                    f"record skill_loaded=true via the orchestrator/CLI before completion."
                ),
            )
        )
    return issues


def _audit_gate_tampering(
    loc: ChangeLocation,
    report: WorkflowStatus,
    schema: WorkflowSchema,
) -> list[AuditIssue]:
    issues: list[AuditIssue] = []
    events = read_events(loc.path)
    issues.extend(_audit_human_decision_evidence(loc, events))
    last_verdict = _latest_gate_verdicts(events)

    for phase in report.phases:
        if phase.status not in {"done", "stopped"}:
            continue
        if not phase.gate:
            continue
        if phase.gate not in schema.gates:
            continue
        verdict_event = last_verdict.get(phase.gate)
        if not verdict_event:
            continue
        reads = verdict_event.get("reads_sha256")
        if not isinstance(reads, dict):
            continue
        if verdict_event.get("verdict") not in SETTLED_GATE_VERDICTS:
            continue
        for rel_path, recorded in reads.items():
            if not isinstance(rel_path, str) or not isinstance(recorded, str):
                continue
            if not is_audited_gate_read(rel_path):
                continue
            current = sha256_file(resolve_change_path(loc, rel_path))
            if current and current != recorded:
                issues.append(
                    AuditIssue(
                        code="ARTIFACT-TAMPERED",
                        phase=phase.id,
                        message=f"ARTIFACT-TAMPERED: {phase.id} {rel_path}",
                    )
                )
    return issues


def _audit_human_decision_evidence(
    loc: ChangeLocation,
    events: list[dict[str, object]],
) -> list[AuditIssue]:
    issues: list[AuditIssue] = []
    for event in events:
        if event.get("type") != "human_decision":
            continue
        evidence_file = event.get("evidence_file") or event.get("review_file")
        evidence_sha = event.get("evidence_sha256") or event.get("review_sha256")
        if not isinstance(evidence_file, str) or not isinstance(evidence_sha, str):
            continue
        current = sha256_file(resolve_change_path(loc, evidence_file))
        if current and current != evidence_sha:
            checkpoint = str(event.get("checkpoint") or "")
            issues.append(
                AuditIssue(
                    code="ARTIFACT-TAMPERED",
                    phase=checkpoint or None,
                    message=f"ARTIFACT-TAMPERED: {checkpoint} {evidence_file}",
                )
            )
    return issues


def _phase_requires_skill_load(phase: str, state: dict[str, Any]) -> bool:
    if phase in SKILL_LOAD_EXEMPT_PHASES:
        return False
    status = state.get("status")
    if not isinstance(status, str):
        return False
    # Healing aggregate judgments are recorded by `aa state heal` (orchestrator),
    # not by a skill phase commit — they never set skill_loaded. All terminal
    # healing statuses must be exempt or report/archive dispatch is blocked.
    if phase in {"healing"} and status in {
        "not_needed",
        "skipped",
        "pending",
        "resolved",
        "exhausted",
        "failed",
    }:
        return False
    if phase == "archive" and status in {"eligible", "not_eligible", "skipped"}:
        return False
    return status in TERMINAL_SKILL_STATUSES


def _audit_reclassification_events(change_dir: Path) -> list[AuditIssue]:
    analysis_path = change_dir / "inspect" / "failure-analysis.json"
    if not analysis_path.exists():
        return []
    try:
        analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(analysis, dict) or not isinstance(analysis.get("failures"), list):
        return []
    events = [e for e in read_events(change_dir) if e.get("type") == "failure_reclassified"]
    issues: list[AuditIssue] = []
    for failure in analysis["failures"]:
        if not isinstance(failure, dict) or not isinstance(failure.get("reclassified"), dict):
            continue
        failure_id = _first_string(failure.get("id"), failure.get("case_id"), failure.get("test"))
        has_event = any(
            e.get("failure") in {failure_id, failure.get("case_id"), failure.get("test")} for e in events
        )
        if not has_event:
            issues.append(
                AuditIssue(
                    code="RECLASSIFY-WITHOUT-EVENT",
                    phase="inspect",
                    message=(
                        f"RECLASSIFY-WITHOUT-EVENT: {failure_id or 'unknown failure'} has "
                        f"reclassified metadata without failure_reclassified event"
                    ),
                )
            )
    return issues


def _audit_verdict_transitions(
    loc: ChangeLocation,
    schema: WorkflowSchema,
) -> list[AuditIssue]:
    events = read_events(loc.path)
    issues: list[AuditIssue] = []
    repair_by_review = _build_repair_map(schema)

    by_gate: dict[str, list[dict[str, object]]] = {}
    for event in events:
        if event.get("type") != "gate_verdict":
            continue
        gate_id = event.get("gate")
        if not isinstance(gate_id, str):
            continue
        by_gate.setdefault(gate_id, []).append(event)

    for gate_id, verdicts in by_gate.items():
        for i in range(1, len(verdicts)):
            prev = verdicts[i - 1]
            nxt = verdicts[i]
            streak_start = i - 1
            while streak_start > 0 and verdicts[streak_start - 1].get("verdict") == prev.get("verdict"):
                streak_start -= 1
            issue = _check_verdict_migration(
                events,
                schema,
                gate_id,
                prev,
                nxt,
                repair_by_review,
                event_seq(verdicts[streak_start]),
            )
            if issue:
                issues.append(issue)
    return issues


def _check_verdict_migration(
    events: list[dict[str, object]],
    schema: WorkflowSchema,
    gate_id: str,
    prev: dict[str, object],
    nxt: dict[str, object],
    repair_by_review: dict[str, str],
    repair_since_seq: int,
) -> AuditIssue | None:
    from_v = str(prev.get("verdict") or "")
    to_v = str(nxt.get("verdict") or "")

    if from_v == "reject":
        return AuditIssue(
            code="GATE-TRANSITION-ILLEGAL",
            message=f"GATE-TRANSITION-ILLEGAL: {gate_id} reject→{to_v}",
        )

    between_repair = [e for e in events if repair_since_seq < event_seq(e) < event_seq(nxt)]
    between_adjacent = [e for e in events if event_seq(prev) < event_seq(e) < event_seq(nxt)]
    review_phase = _find_review_phase_for_gate(schema, gate_id)
    repair_phase = repair_by_review.get(review_phase) if review_phase else None
    repair_done = False
    if repair_phase:
        repair_done = any(
            (e.get("type") == "phase_transition" and e.get("phase") == repair_phase and e.get("to") == "done")
            or (e.get("type") == "dispatch_signed" and e.get("phase") == repair_phase)
            or (e.get("type") == "phase_dispatched" and e.get("phase") == repair_phase)
            or (e.get("type") == "phase_outcome_committed" and e.get("phase") == repair_phase)
            for e in between_repair
        )

    latest_decision = _latest_matching_gate_decision(between_adjacent, schema, gate_id)
    accept_risk = _is_valid_accept_risk_decision(latest_decision, schema, gate_id, nxt)

    if from_v == "needs_fix" and to_v == "pass" and not repair_done:
        return AuditIssue(
            code="GATE-TRANSITION-ILLEGAL",
            message=f"GATE-TRANSITION-ILLEGAL: {gate_id} needs_fix→pass",
        )

    if from_v == "needs_human_review" and to_v == "pass" and not accept_risk and not repair_done:
        return AuditIssue(
            code="GATE-TRANSITION-ILLEGAL",
            message=f"GATE-TRANSITION-ILLEGAL: {gate_id} needs_human_review→pass",
        )

    if from_v == "pass" and to_v == "pass":
        prev_raw = prev.get("reads_sha256")
        next_raw = nxt.get("reads_sha256")
        prev_hash: dict[str, Any] = prev_raw if isinstance(prev_raw, dict) else {}
        next_hash: dict[str, Any] = next_raw if isinstance(next_raw, dict) else {}
        hash_changed = any(k in prev_hash and prev_hash[k] != next_hash.get(k) for k in next_hash)
        repair_done_adjacent = False
        if repair_phase:
            repair_done_adjacent = any(
                e.get("type") == "phase_transition"
                and e.get("phase") == repair_phase
                and e.get("to") == "done"
                for e in between_adjacent
            )
        healing_refresh = _is_safety_gate(schema, gate_id) and any(
            (e.get("type") == "phase_transition" and e.get("phase") in {"healing-rerun", "healing-reinspect"})
            or e.get("type") == "execution_start"
            for e in between_adjacent
        )
        if hash_changed and not repair_done_adjacent and not healing_refresh:
            return AuditIssue(
                code="GATE-TRANSITION-ILLEGAL",
                message=f"GATE-TRANSITION-ILLEGAL: {gate_id} pass→pass (content changed)",
            )
    return None


def _latest_matching_gate_decision(
    events: list[dict[str, object]],
    schema: WorkflowSchema,
    gate_id: str,
) -> dict[str, object] | None:
    for event in reversed(events):
        if event.get("source") != "decide" or event.get("type") != "human_decision":
            continue
        checkpoint = event.get("checkpoint")
        if checkpoint == gate_id:
            return event
        if isinstance(checkpoint, str):
            phase = next((p for p in schema.phases if p.id == checkpoint), None)
            if phase and phase.gate == gate_id:
                return event
    return None


def _is_valid_accept_risk_decision(
    event: dict[str, object] | None,
    schema: WorkflowSchema,
    gate_id: str,
    nxt: dict[str, object],
) -> bool:
    if event is None:
        return False
    if event.get("action") != "accept_risk":
        return False
    reason = event.get("reason")
    who = event.get("who")
    review_file = event.get("review_file")
    review_sha = event.get("review_sha256")
    if not isinstance(reason, str) or not reason.strip():
        return False
    if not isinstance(who, str) or not who.strip():
        return False
    if not isinstance(review_file, str) or not isinstance(review_sha, str):
        return False
    checkpoint = event.get("checkpoint")
    if isinstance(checkpoint, str):
        phase = next((p for p in schema.phases if p.id == checkpoint), None)
        if checkpoint != gate_id and not (phase and phase.gate == gate_id):
            return False
    next_reads = nxt.get("reads_sha256")
    return isinstance(next_reads, dict) and next_reads.get(review_file) == review_sha


def _is_safety_gate(schema: WorkflowSchema, gate_id: str) -> bool:
    gate = schema.gates.get(gate_id)
    if gate is None:
        return False
    return any(
        r.path
        in {
            "healing/fixer-safety-check.json",
            "inspect/inspect-safety-check.json",
        }
        for r in gate.reads
    )


def _latest_gate_verdicts(
    events: list[dict[str, object]],
) -> dict[str, dict[str, object]]:
    latest: dict[str, dict[str, object]] = {}
    for event in events:
        if event.get("type") != "gate_verdict":
            continue
        gate = event.get("gate")
        if isinstance(gate, str):
            latest[gate] = event
    return latest


def _build_repair_map(schema: WorkflowSchema) -> dict[str, str]:
    return {p.repair_of: p.id for p in schema.phases if p.repair_of}


def _find_review_phase_for_gate(schema: WorkflowSchema, gate_id: str) -> str | None:
    for phase in schema.phases:
        if phase.gate == gate_id:
            return phase.id
    return None


def _first_string(*values: object) -> str | None:
    for value in values:
        if isinstance(value, str) and value:
            return value
    return None


def check_verdict_migration(
    events: list[dict[str, object]],
    schema: WorkflowSchema,
    gate_id: str,
    prev: dict[str, object],
    nxt: dict[str, object],
    repair_by_review: dict[str, str],
    repair_since_seq: int,
) -> AuditIssue | None:
    return _check_verdict_migration(events, schema, gate_id, prev, nxt, repair_by_review, repair_since_seq)


def build_repair_map(schema: WorkflowSchema) -> dict[str, str]:
    return _build_repair_map(schema)
