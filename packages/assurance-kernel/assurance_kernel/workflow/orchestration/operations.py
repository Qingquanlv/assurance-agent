"""Shared domain operations that own progression commits.

Driver and CLI adapters call these; they alone stage strict events and state
updates through ``workflow.core.progression.transaction``.

v1 phase dispatch / outcome / heal-transition writers were removed with the
GraphRuntime cutover. Remaining surface: human decisions and shared healing
constants used by graph operation handlers.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

import yaml

from assurance_kernel import resources
from assurance_kernel.artifacts.models import WorkflowState
from assurance_kernel.change_location import ChangeLocation
from assurance_kernel.exceptions import AaError
from assurance_kernel.workflow.core.audit_scope import is_audited_gate_read
from assurance_kernel.workflow.core.override_paths import DECISION_REL_PATH, TOKEN_REL_PATH
from assurance_kernel.workflow.core.product_hooks import current_product_hooks
from assurance_kernel.workflow.core.progression import transaction
from assurance_kernel.workflow.core.tree_hash import hash_test_tree, sha256_file
from assurance_kernel.workflow.orchestration.decision_support import resolve_decision_support
from assurance_kernel.workflow.orchestration.gates import resolve_change_path
from assurance_kernel.workflow.orchestration.schema import GateDef, normalize_gates

HEAL_STATUSES = frozenset({"resolved", "exhausted", "not_needed", "failed", "skipped"})
HUMAN_DECISION_ACTIONS = frozenset(
    {"fix_and_proceed", "accept_risk", "stop", "allow_test_changes", "skip_branch"}
)
BASELINE_REL = "healing/entry-baseline.json"
_TERMINAL_GRAPH_EVENTS = frozenset({"graph_completed", "graph_stopped", "graph_failed"})


class _GatesOnly:
    def __init__(self, gates: dict[str, GateDef]) -> None:
        self.gates = gates


def _load_gates(project_root: Path) -> _GatesOnly:
    """Load gate defs without importing ``workflow.graph`` (layer boundary)."""
    for rel in (Path(".aa/workflow-schema.yaml"), Path("schemas/workflow-schema.yaml")):
        candidate = project_root / rel
        if candidate.exists():
            doc = yaml.safe_load(candidate.read_text(encoding="utf-8"))
            if not isinstance(doc, dict):
                raise AaError(f"invalid workflow schema: {candidate}")
            return _GatesOnly(normalize_gates(doc.get("gates")))
    doc = yaml.safe_load(resources.read_text("schemas", "workflow-schema.yaml"))
    if not isinstance(doc, dict):
        raise AaError("invalid packaged workflow schema")
    return _GatesOnly(normalize_gates(doc.get("gates")))


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

    schema = _load_gates(project_root)
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
        hooks = current_product_hooks()
        integrity = hooks.assert_test_tree_unchanged_or_healing(
            project_root,
            loc.change_id,
            allow_test_changes=True,
        )
        hooks.assert_test_changes_override_allowed(
            change_dir,
            integrity,
            hooks.load_test_changes_override_policy(project_root),
        )
        token = hooks.build_test_changes_override_token(
            change_dir,
            change_id=loc.change_id,
            reason=reason,
            tests_tree_sha256=hash_test_tree(project_root).aggregate,
        )
        override_token_bytes = hooks.token_json_bytes(token)
        evidence_file = DECISION_REL_PATH.as_posix()
        evidence_sha256 = hashlib.sha256(override_token_bytes).hexdigest()

    review_file: str | None = None
    review_sha256: str | None = None
    if checkpoint == "healing.safety" and action == "accept_risk":
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

        if action == "stop":
            for event in txn.ledger.all():
                if event.get("type") in _TERMINAL_GRAPH_EVENTS:
                    raise AaError(f"workflow already terminal ({event.get('type')})")
            data["terminal"] = {"kind": "stopped", "reason": reason}

        record: dict[str, object] = {
            "checkpoint": checkpoint,
            "action": action,
            "reason": reason,
            "who": who,
            "at": datetime.now(timezone.utc).isoformat(),
        }
        decisions.append(record)
        data["decisions"] = decisions

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
