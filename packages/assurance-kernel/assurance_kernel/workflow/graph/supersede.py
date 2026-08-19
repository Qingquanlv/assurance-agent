"""Audited legacy-root supersede and single-use v6 replacement (D18 / Task 13).

Eligibility is typed — never inferred from free-form error text. Uses Task 7's
frozen ``RootEffectFenceStore`` guard/prepare/commit protocol. Ordinary
active-root and ``restart: once`` guards remain; only an unused replacement
authorization pair bypasses them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from assurance_kernel.artifacts.canonical import canonical_json_bytes, sha256_bytes
from assurance_kernel.artifacts.models.common import StrictWireModel
from assurance_kernel.exceptions import AaError
from assurance_kernel.workflow.core.graph_events import GraphInvocationSupersededEvent
from assurance_kernel.workflow.graph.compiler import PinnedDefinitionRequest, canonical_digest
from assurance_kernel.workflow.graph.effect_retry import RootEffectFenceStore
from assurance_kernel.workflow.graph.leases import LeaseRegistry
from assurance_kernel.workflow.graph.models import GraphProjection
from assurance_kernel.workflow.graph.project_locks import ProjectPublicationStore
from assurance_kernel.workflow.graph.resume_compatibility import (
    LEGACY_COMMIT_SAFETY_SEMANTICS_UNBOUND,
    ResumeCompatibilityDecision,
)
from assurance_kernel.workflow.graph.status import pending_write_sets, unacknowledged_durable_effects

SupersedeAction = Literal["rerun-v6", "stop"]

SupersedeIneligibilityReason = Literal[
    "not_root",
    "wrong_entrypoint",
    "not_latest_root",
    "already_terminal",
    "not_legacy_blocked",
    "subtree_not_quiescent",
    "stop_with_params",
    "invalid_params",
    "staged_request_not_v6",
    "conflicting_supersede",
    "missing_who_or_reason",
    "params_not_allowed",
]


class SupersedeError(AaError):
    """Typed supersede refusal or conflict; CLI maps to a stable nonzero exit."""

    def __init__(self, reason_code: str, message: str | None = None) -> None:
        self.reason_code = reason_code
        super().__init__(message or reason_code)


class SupersedeEligibility(StrictWireModel):
    schema_version: Literal["1"] = "1"
    eligible: bool
    reason: SupersedeIneligibilityReason | None = None
    detail: str | None = None
    root_invocation_id: str = ""
    entrypoint: str = ""
    descendant_invocation_ids: tuple[str, ...] = ()
    subtree_digest: str = ""
    source_sequence: int = 0
    resume_reason: str | None = None


class SupersedeResult(StrictWireModel):
    schema_version: Literal["1"] = "1"
    superseded_invocation_id: str
    supersede_id: str
    action: SupersedeAction
    replacement_invocation_id: str | None = None
    replacement_authorization_id: str | None = None
    exit_code: Literal[0, 40] = 0
    reason: str = "superseded"


@dataclass(frozen=True, slots=True)
class StagedReplacementPlan:
    """Validated current-v6 request + resolved params authorized for rerun."""

    request: PinnedDefinitionRequest
    definition_request_digest: str
    params: dict[str, object]
    params_sha256: str


def definition_request_digest(request: PinnedDefinitionRequest) -> str:
    payload = {
        "graph_digest": request.graph_digest,
        "ingest_catalog_digest": request.ingest_catalog_digest,
        "contract_digests": list(request.contract_digests),
        "event_schema_version": request.event_schema_version,
        "gate_semantics_digest": request.gate_semantics_digest,
        "assurance_profile_digest": request.assurance_profile_digest,
        "gate_semantics_object_id": request.gate_semantics_object_id,
        "topology_safety_semantics_object_id": request.topology_safety_semantics_object_id,
        "topology_safety_semantics_digest": request.topology_safety_semantics_digest,
        "commit_safety_semantics_object_id": request.commit_safety_semantics_object_id,
        "commit_safety_semantics_digest": request.commit_safety_semantics_digest,
    }
    return sha256_bytes(canonical_json_bytes(payload))


def descendant_invocation_closure(
    events: list[dict[str, object]],
    root_invocation_id: str,
) -> tuple[str, ...]:
    """Canonical sorted descendant invocation IDs under ``root_invocation_id``."""
    children: dict[str, list[str]] = {}
    for raw in events:
        if raw.get("source") != "graph" or raw.get("type") != "graph_invocation_started":
            continue
        inv = raw.get("invocation_id")
        parent = raw.get("parent_invocation_id")
        if not isinstance(inv, str) or not isinstance(parent, str) or not parent:
            continue
        children.setdefault(parent, []).append(inv)
    out: list[str] = []
    stack = list(children.get(root_invocation_id, ()))
    seen: set[str] = set()
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        out.append(current)
        stack.extend(children.get(current, ()))
    return tuple(sorted(out))


def subtree_digest_for(
    *,
    root_invocation_id: str,
    descendant_invocation_ids: tuple[str, ...],
    source_sequence: int,
) -> str:
    payload = {
        "root_invocation_id": root_invocation_id,
        "descendant_invocation_ids": list(descendant_invocation_ids),
        "source_sequence": source_sequence,
    }
    return sha256_bytes(canonical_json_bytes(payload))


def compute_supersede_id(
    *,
    root_invocation_id: str,
    entrypoint: str,
    action: SupersedeAction,
    reason_code: str,
    who: str,
    reason: str,
    params_sha256: str | None,
    definition_request_digest: str | None,
    descendant_invocation_ids: tuple[str, ...],
    subtree_digest: str,
    source_sequence: int,
) -> str:
    payload = {
        "root_invocation_id": root_invocation_id,
        "entrypoint": entrypoint,
        "action": action,
        "reason_code": reason_code,
        "who": who,
        "reason": reason,
        "params_sha256": params_sha256,
        "definition_request_digest": definition_request_digest,
        "descendant_invocation_ids": list(descendant_invocation_ids),
        "subtree_digest": subtree_digest,
        "source_sequence": source_sequence,
    }
    return sha256_bytes(canonical_json_bytes(payload))


def compute_replacement_authorization_id(supersede_id: str) -> str:
    return sha256_bytes(canonical_json_bytes({"supersede_id": supersede_id, "kind": "replacement"}))


def find_supersede_event(
    events: list[dict[str, object]],
    root_invocation_id: str,
) -> GraphInvocationSupersededEvent | None:
    found: GraphInvocationSupersededEvent | None = None
    for raw in events:
        if raw.get("source") != "graph" or raw.get("type") != "graph_invocation_superseded":
            continue
        if raw.get("invocation_id") != root_invocation_id:
            continue
        payload = {k: v for k, v in raw.items() if k not in {"seq", "ts"}}
        event = GraphInvocationSupersededEvent.model_validate(payload)
        if found is not None:
            if found.model_dump(mode="json") != event.model_dump(mode="json"):
                raise SupersedeError(
                    "conflicting_supersede",
                    f"conflicting graph_invocation_superseded payloads for {root_invocation_id}",
                )
            continue
        found = event
    return found


def find_replacement_root(
    events: list[dict[str, object]],
    *,
    supersedes_invocation_id: str,
    replacement_authorization_id: str,
) -> str | None:
    for raw in events:
        if raw.get("source") != "graph" or raw.get("type") != "graph_invocation_started":
            continue
        if raw.get("supersedes_invocation_id") != supersedes_invocation_id:
            continue
        if raw.get("replacement_authorization_id") != replacement_authorization_id:
            continue
        inv = raw.get("invocation_id")
        if isinstance(inv, str):
            return inv
    return None


def authorization_consumed(
    events: list[dict[str, object]],
    replacement_authorization_id: str,
) -> bool:
    for raw in events:
        if raw.get("source") != "graph" or raw.get("type") != "graph_invocation_started":
            continue
        if raw.get("replacement_authorization_id") == replacement_authorization_id:
            return True
    return False


def evaluate_subtree_quiescence(
    *,
    project_root: Path,
    change_dir: Path,
    root_invocation_id: str,
    descendant_invocation_ids: tuple[str, ...],
    events: list[dict[str, object]],
) -> str | None:
    """Return a quiescence detail string when the subtree is not quiescent."""
    closure = (root_invocation_id, *descendant_invocation_ids)
    closure_set = set(closure)

    leases = LeaseRegistry(change_dir).read_all()
    for lease in leases.values():
        # Leases are task-scoped; reject any live lease whose invocation is in closure.
        inv = getattr(lease, "invocation_id", None)
        if isinstance(inv, str) and inv in closure_set:
            return f"live_lease:{lease.task_id}"
        # Fallback: task_id prefixes sometimes embed invocation; also scan ledger running tasks.
    for inv_id in closure:
        for raw in events:
            if raw.get("source") != "graph" or raw.get("invocation_id") != inv_id:
                continue
            if raw.get("type") != "task_attempt_started":
                continue
            task_id = raw.get("task_id")
            attempt_id = raw.get("attempt_id")
            if not isinstance(task_id, str):
                continue
            lease = leases.get(task_id)
            if lease is not None and (attempt_id is None or lease.attempt_id == attempt_id):
                return f"live_lease:{task_id}"

    for inv_id in closure:
        # Open/running attempts from projection-equivalent event scan.
        open_started: set[str] = set()
        finished: set[str] = set()
        for raw in events:
            if raw.get("source") != "graph" or raw.get("invocation_id") != inv_id:
                continue
            etype = raw.get("type")
            attempt_id = raw.get("attempt_id")
            if not isinstance(attempt_id, str):
                continue
            if etype == "task_attempt_started":
                open_started.add(attempt_id)
            elif etype in {
                "task_attempt_succeeded",
                "task_attempt_failed",
                "task_attempt_stopped",
                "task_attempt_abandoned",
            }:
                finished.add(attempt_id)
        still_open = open_started - finished
        if still_open:
            return f"open_attempt:{sorted(still_open)[0]}"

        pending_ws = pending_write_sets(events, inv_id)
        if pending_ws:
            return f"pending_write_set:{pending_ws[0]}"

        # Prepared/uncommitted superstep: planned without matching commit after it.
        last_planned_seq: int | None = None
        last_committed_seq: int | None = None
        for raw in events:
            if raw.get("source") != "graph" or raw.get("invocation_id") != inv_id:
                continue
            seq = raw.get("seq")
            if not isinstance(seq, int):
                continue
            if raw.get("type") == "superstep_planned":
                last_planned_seq = seq
            elif raw.get("type") == "superstep_committed":
                last_committed_seq = seq
        if last_planned_seq is not None and (
            last_committed_seq is None or last_committed_seq < last_planned_seq
        ):
            return f"uncommitted_superstep:{inv_id}"

        pending_effects = unacknowledged_durable_effects(events, inv_id)
        if pending_effects:
            return f"unacknowledged_effect:{pending_effects[0]}"

        pub_store = ProjectPublicationStore(project_root)
        if pub_store.has_unacknowledged(inv_id):
            return f"pending_publication:{inv_id}"

    retry_root = project_root / "qa" / ".graph-runtime" / "effect-retries"
    if retry_root.is_dir():
        for path in sorted(retry_root.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                root_id = payload.get("root_invocation_id")
                effect_id = payload.get("effect_id") or path.stem
            except Exception:  # noqa: BLE001 — malformed sidecar blocks supersede
                return f"active_retry_sidecar:{path.stem}"
            if isinstance(root_id, str) and root_id in closure_set:
                return f"active_retry_sidecar:{effect_id}"

    # Concurrent child creation: any started child of root not in recorded closure
    # cannot occur under the progression lock if we rescan; treat unexpected child
    # starts whose parent is in closure but id not listed as non-quiescent.
    recorded = set(descendant_invocation_ids)
    for raw in events:
        if raw.get("source") != "graph" or raw.get("type") != "graph_invocation_started":
            continue
        parent = raw.get("parent_invocation_id")
        inv = raw.get("invocation_id")
        if parent == root_invocation_id and isinstance(inv, str) and inv not in recorded:
            return f"concurrent_child:{inv}"
        if isinstance(parent, str) and parent in recorded and isinstance(inv, str) and inv not in recorded:
            return f"concurrent_child:{inv}"

    return None


def evaluate_supersede_eligibility(
    *,
    projection: GraphProjection,
    latest_root_id: str | None,
    expected_entrypoint: str | None,
    decision: ResumeCompatibilityDecision | None,
    action: SupersedeAction,
    who: str,
    reason: str,
    params_provided: bool,
    staged_request: PinnedDefinitionRequest | None,
    project_root: Path,
    change_dir: Path,
    events: list[dict[str, object]],
) -> SupersedeEligibility:
    """Typed eligibility scan. Never parses exception text."""
    root_id = projection.invocation_id
    descendants = descendant_invocation_closure(events, root_id)
    source_sequence = projection.event_seq
    digest = subtree_digest_for(
        root_invocation_id=root_id,
        descendant_invocation_ids=descendants,
        source_sequence=source_sequence,
    )
    base = {
        "root_invocation_id": root_id,
        "entrypoint": projection.entrypoint,
        "descendant_invocation_ids": descendants,
        "subtree_digest": digest,
        "source_sequence": source_sequence,
    }

    if not who.strip() or not reason.strip():
        return SupersedeEligibility(eligible=False, reason="missing_who_or_reason", **base)
    if projection.parent_invocation_id is not None:
        return SupersedeEligibility(eligible=False, reason="not_root", **base)
    if expected_entrypoint is not None and projection.entrypoint != expected_entrypoint:
        return SupersedeEligibility(eligible=False, reason="wrong_entrypoint", **base)
    if latest_root_id is not None and latest_root_id != root_id:
        return SupersedeEligibility(eligible=False, reason="not_latest_root", **base)
    if projection.terminal is not None:
        # Already-superseded exact retry is handled by the caller via event lookup.
        return SupersedeEligibility(eligible=False, reason="already_terminal", **base)
    if action == "stop" and params_provided:
        return SupersedeEligibility(eligible=False, reason="stop_with_params", **base)
    if action == "rerun-v6" and params_provided and staged_request is None:
        # Caller validates params separately; missing staged request is not_v6/invalid.
        pass
    if decision is None or decision.allowed or decision.reason != LEGACY_COMMIT_SAFETY_SEMANTICS_UNBOUND:
        return SupersedeEligibility(
            eligible=False,
            reason="not_legacy_blocked",
            resume_reason=None if decision is None else decision.reason,
            **base,
        )
    if action == "rerun-v6":
        if staged_request is None:
            return SupersedeEligibility(eligible=False, reason="staged_request_not_v6", **base)
        if staged_request.event_schema_version != 6:
            return SupersedeEligibility(eligible=False, reason="staged_request_not_v6", **base)

    quiescence = evaluate_subtree_quiescence(
        project_root=project_root,
        change_dir=change_dir,
        root_invocation_id=root_id,
        descendant_invocation_ids=descendants,
        events=events,
    )
    if quiescence is not None:
        return SupersedeEligibility(
            eligible=False,
            reason="subtree_not_quiescent",
            detail=quiescence,
            resume_reason=LEGACY_COMMIT_SAFETY_SEMANTICS_UNBOUND,
            **base,
        )

    return SupersedeEligibility(
        eligible=True,
        resume_reason=LEGACY_COMMIT_SAFETY_SEMANTICS_UNBOUND,
        **base,
    )


def build_supersede_event(
    *,
    eligibility: SupersedeEligibility,
    action: SupersedeAction,
    who: str,
    reason: str,
    event_schema_version: int,
    checkpoint_ns: str,
    staged: StagedReplacementPlan | None,
) -> GraphInvocationSupersededEvent:
    if not eligibility.eligible:
        raise SupersedeError(eligibility.reason or "not_eligible")
    params_sha256 = None if staged is None else staged.params_sha256
    def_digest = None if staged is None else staged.definition_request_digest
    supersede_id = compute_supersede_id(
        root_invocation_id=eligibility.root_invocation_id,
        entrypoint=eligibility.entrypoint,
        action=action,
        reason_code=LEGACY_COMMIT_SAFETY_SEMANTICS_UNBOUND,
        who=who,
        reason=reason,
        params_sha256=params_sha256,
        definition_request_digest=def_digest,
        descendant_invocation_ids=eligibility.descendant_invocation_ids,
        subtree_digest=eligibility.subtree_digest,
        source_sequence=eligibility.source_sequence,
    )
    replacement_id = None if action == "stop" else compute_replacement_authorization_id(supersede_id)
    return GraphInvocationSupersededEvent(
        type="graph_invocation_superseded",
        invocation_id=eligibility.root_invocation_id,
        checkpoint_ns=checkpoint_ns,
        entrypoint=eligibility.entrypoint,
        supersede_id=supersede_id,
        reason_code="legacy_commit_safety_semantics_unbound",
        who=who,
        reason=reason,
        action=action,
        params_sha256=params_sha256,
        definition_request_digest=def_digest,
        replacement_authorization_id=replacement_id,
        descendant_invocation_ids=list(eligibility.descendant_invocation_ids),
        subtree_digest=eligibility.subtree_digest,
        source_sequence=eligibility.source_sequence,
        event_schema_version=event_schema_version,
    )


def stage_definition_request_record(
    change_dir: Path,
    *,
    digest: str,
    request: PinnedDefinitionRequest,
) -> Path:
    """Persist staged request JSON under change runtime for exact retry reload."""
    rel = Path(".graph-runtime") / "supersede-requests" / f"{digest}.json"
    path = change_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "graph_digest": request.graph_digest,
        "ingest_catalog_digest": request.ingest_catalog_digest,
        "contract_digests": list(request.contract_digests),
        "event_schema_version": request.event_schema_version,
        "gate_semantics_digest": request.gate_semantics_digest,
        "assurance_profile_digest": request.assurance_profile_digest,
        "gate_semantics_object_id": request.gate_semantics_object_id,
        "topology_safety_semantics_object_id": request.topology_safety_semantics_object_id,
        "topology_safety_semantics_digest": request.topology_safety_semantics_digest,
        "commit_safety_semantics_object_id": request.commit_safety_semantics_object_id,
        "commit_safety_semantics_digest": request.commit_safety_semantics_digest,
    }
    raw = (json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode(
        "utf-8"
    )
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(raw)
    tmp.replace(path)
    return path


def load_staged_definition_request(change_dir: Path, digest: str) -> PinnedDefinitionRequest:
    path = change_dir / ".graph-runtime" / "supersede-requests" / f"{digest}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    contracts = payload["contract_digests"]
    return PinnedDefinitionRequest(
        graph_digest=str(payload["graph_digest"]),
        ingest_catalog_digest=str(payload["ingest_catalog_digest"]),
        contract_digests=tuple((str(a), str(b)) for a, b in contracts),
        event_schema_version=int(payload["event_schema_version"]),
        gate_semantics_digest=str(payload["gate_semantics_digest"]),
        assurance_profile_digest=str(payload["assurance_profile_digest"]),
        gate_semantics_object_id=str(payload.get("gate_semantics_object_id") or ""),
        topology_safety_semantics_object_id=str(payload.get("topology_safety_semantics_object_id") or ""),
        topology_safety_semantics_digest=str(payload.get("topology_safety_semantics_digest") or ""),
        commit_safety_semantics_object_id=str(payload.get("commit_safety_semantics_object_id") or ""),
        commit_safety_semantics_digest=str(payload.get("commit_safety_semantics_digest") or ""),
    )


def recover_prepared_fence(
    fence_store: RootEffectFenceStore,
    *,
    root_invocation_id: str,
    events: list[dict[str, object]],
) -> GraphInvocationSupersededEvent | None:
    """Resolve a prepared fence against the authoritative supersede event."""
    state = fence_store.load(root_invocation_id)
    event = find_supersede_event(events, root_invocation_id)
    if state is None:
        return event
    if state.status == "committed":
        return event
    # prepared
    if event is not None:
        fence_store.commit_terminal(root_invocation_id)
        return event
    fence_store.abort_prepared(root_invocation_id)
    return None


def fence_blocks_invocation(
    events: list[dict[str, object]],
    invocation_id: str,
) -> GraphInvocationSupersededEvent | None:
    """Return the supersede event fencing this invocation (root or descendant)."""
    for raw in events:
        if raw.get("source") != "graph" or raw.get("type") != "graph_invocation_superseded":
            continue
        payload = {k: v for k, v in raw.items() if k not in {"seq", "ts"}}
        event = GraphInvocationSupersededEvent.model_validate(payload)
        if event.invocation_id == invocation_id or invocation_id in event.descendant_invocation_ids:
            return event
    return None


def build_staged_replacement_plan(
    *,
    request: PinnedDefinitionRequest,
    params: dict[str, object],
) -> StagedReplacementPlan:
    if request.event_schema_version != 6:
        raise SupersedeError("staged_request_not_v6", "replacement requires event schema v6")
    digest = definition_request_digest(request)
    return StagedReplacementPlan(
        request=request,
        definition_request_digest=digest,
        params=dict(params),
        params_sha256=canonical_digest(params),
    )


__all__ = [
    "SupersedeAction",
    "SupersedeEligibility",
    "SupersedeError",
    "SupersedeResult",
    "StagedReplacementPlan",
    "authorization_consumed",
    "build_staged_replacement_plan",
    "build_supersede_event",
    "compute_replacement_authorization_id",
    "compute_supersede_id",
    "definition_request_digest",
    "descendant_invocation_closure",
    "evaluate_subtree_quiescence",
    "evaluate_supersede_eligibility",
    "fence_blocks_invocation",
    "find_replacement_root",
    "find_supersede_event",
    "load_staged_definition_request",
    "recover_prepared_fence",
    "stage_definition_request_record",
    "subtree_digest_for",
]
