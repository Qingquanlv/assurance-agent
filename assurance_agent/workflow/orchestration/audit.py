"""Read-side audit helpers retained after the v1 phase engine deletion.

Gate verdict migration checks no longer walk ``schema.phases``; repair
authorization is ledger-event based (graph task outcomes / human decisions).
``run_status_audits`` / ``WorkflowStatus`` projection audits were removed with
the v1 engine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from assurance_agent.workflow.core.events import event_seq
from assurance_agent.workflow.orchestration.schema import GateDef


@dataclass(frozen=True)
class AuditIssue:
    code: str
    message: str
    phase: str | None = None


@dataclass
class AuditResult:
    issues: list[AuditIssue] = field(default_factory=list)


class _SchemaWithGates(Protocol):
    gates: dict[str, GateDef]


def check_verdict_migration(
    events: list[dict[str, object]],
    schema: _SchemaWithGates,
    gate_id: str,
    prev: dict[str, object],
    nxt: dict[str, object],
    repair_by_review: dict[str, str],
    repair_since_seq: int,
) -> AuditIssue | None:
    return _check_verdict_migration(events, schema, gate_id, prev, nxt, repair_by_review, repair_since_seq)


def build_repair_map(_schema: _SchemaWithGates) -> dict[str, str]:
    """v1 phase.repair_of map; empty under graph schema (repair is graph-routed)."""
    return {}


def _check_verdict_migration(
    events: list[dict[str, object]],
    schema: _SchemaWithGates,
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
    repair_done = any(
        e.get("type")
        in {
            "task_attempt_succeeded",
            "budget_consumed",
            "phase_outcome_committed",
            "dispatch_signed",
        }
        for e in between_repair
    ) or bool(repair_by_review)

    latest_decision = _latest_matching_gate_decision(between_adjacent, gate_id)
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
        healing_refresh = _is_safety_gate(schema, gate_id) and any(
            e.get("type")
            in {
                "task_attempt_succeeded",
                "graph_resumed",
                "execution_start",
                "phase_transition",
            }
            for e in between_adjacent
        )
        if hash_changed and not healing_refresh:
            return AuditIssue(
                code="GATE-TRANSITION-ILLEGAL",
                message=f"GATE-TRANSITION-ILLEGAL: {gate_id} pass→pass (content changed)",
            )
    return None


def _latest_matching_gate_decision(
    events: list[dict[str, object]],
    gate_id: str,
) -> dict[str, object] | None:
    for event in reversed(events):
        if event.get("source") != "decide" or event.get("type") != "human_decision":
            continue
        if event.get("checkpoint") == gate_id:
            return event
    return None


def _is_valid_accept_risk_decision(
    event: dict[str, object] | None,
    schema: _SchemaWithGates,
    gate_id: str,
    nxt: dict[str, object],
) -> bool:
    if event is None or event.get("action") != "accept_risk":
        return False
    if event.get("checkpoint") != gate_id and not _is_safety_gate(schema, gate_id):
        return False
    review_sha = event.get("review_sha256")
    if not isinstance(review_sha, str) or not review_sha:
        return False
    reads = nxt.get("reads_sha256")
    if isinstance(reads, dict) and review_sha not in reads.values():
        # Accept-risk may bind a dedicated review artifact rather than a gate read.
        return event.get("review_file") is not None
    return True


def _is_safety_gate(schema: _SchemaWithGates, gate_id: str) -> bool:
    return gate_id in schema.gates and (
        "safety" in gate_id or gate_id.endswith(".safety") or gate_id == "fixer-safety"
    )
