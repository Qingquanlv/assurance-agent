"""Gate-verdict evidence helpers: hash audited reads and build ledger events.

Mirror of TS ``buildGateVerdictEvent`` / ``computeReadsSha256`` in events.ts.
"""

from __future__ import annotations

from typing import Any

from assurance_agent.change_location import ChangeLocation
from assurance_agent.workflow.core.audit_scope import is_audited_gate_read
from assurance_agent.workflow.execution.tree_hash import sha256_file
from assurance_agent.workflow.orchestration.gates import resolve_change_path
from assurance_agent.workflow.orchestration.schema import WorkflowSchema


def compute_reads_sha256(
    schema: WorkflowSchema,
    loc: ChangeLocation,
    gate_id: str,
) -> dict[str, str] | None:
    """Hash audited read paths for a gate. Returns None when nothing hashed."""
    gate = schema.gates.get(gate_id)
    if gate is None:
        return None
    hashes: dict[str, str] = {}
    for read in gate.reads:
        if not is_audited_gate_read(read.path):
            continue
        digest = sha256_file(resolve_change_path(loc, read.path))
        if digest:
            hashes[read.path] = digest
    return hashes or None


def build_gate_verdict_event(
    loc: ChangeLocation,
    schema: WorkflowSchema,
    *,
    phase: str | None,
    gate: str,
    verdict: str,
    matched_rule: str | None = None,
    reason: str | None = None,
    evidence: dict[str, Any] | None = None,
    blocks: int | None = None,
) -> dict[str, object]:
    """Build a ``gate_verdict`` event payload including ``reads_sha256``."""
    event: dict[str, object] = {
        "source": "gate",
        "type": "gate_verdict",
        "phase": phase,
        "gate": gate,
        "verdict": verdict,
        "reads_sha256": compute_reads_sha256(schema, loc, gate),
    }
    if matched_rule is not None:
        event["matched_rule"] = matched_rule
    if reason is not None:
        event["reason"] = reason
    if evidence is not None:
        event["evidence"] = evidence
    if blocks is not None:
        event["blocks"] = blocks
    return event
