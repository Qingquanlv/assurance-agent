from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, cast

from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.frozen_json import thaw_json
from graph_engine.runtime.models import (
    ActivationRecord,
    AttemptRecord,
    GraphInstanceRecord,
    InvocationProjection,
)

from assurance_product.models import (
    AdapterEvidenceRefV1,
    CoverageProgressV1,
    EffectStatusV1,
    GraphStatusV1,
    NodeStatusV1,
    PendingInterruptStatusV1,
    StatusV1,
)

_GRAPH_STATES: dict[str, Literal["inactive", "running", "failed", "stopped", "interrupted", "completed"]] = {
    "running": "running",
    "completed": "completed",
    "failed": "failed",
}
_PRODUCT_STATUS: dict[str, Literal["running", "blocked", "interrupted", "stopped", "failed", "completed"]] = {
    "succeeded": "completed",
    "failed": "failed",
    "stopped": "stopped",
    "running": "running",
}


def project_status_fields(
    projection: InvocationProjection,
    *,
    root_input_digest: str | None = None,
    initial_tree_id: str | None = None,
    selected_test_families: tuple[str, ...] = (),
) -> dict[str, object]:
    if projection.status == "not_started" or projection.invocation_id is None:
        raise ValueError("cannot project status for an unstarted invocation")
    if projection.lock_digest is None or projection.entrypoint is None:
        raise ValueError("started projection requires complete invocation identity")
    families = selected_test_families or _selected_families(projection)
    return {
        "schema_version": "1",
        "invocation_id": projection.invocation_id,
        "lock_digest": projection.lock_digest,
        "root_input_digest": root_input_digest or _require_digest(root_input_digest),
        "initial_tree_id": initial_tree_id or _require_digest(initial_tree_id),
        "current_head_tree_id": projection.head_tree_id or initial_tree_id or _require_digest(None),
        "status": _status_class(projection),
        "entrypoint": projection.entrypoint,
        "graph_hierarchy": tuple(_graph_status(item) for item in projection.graph_instances),
        "node_states": tuple(_node_status(item) for item in projection.activations),
        "selected_test_families": families,
        "coverage_progress": _coverage_progress(projection),
        "durable_effects": tuple(_effect_status(item) for item in projection.effects),
        "adapter_evidence": tuple(_adapter_evidence(projection)),
        "pending_interrupt": _pending_interrupt(projection),
        "terminal_reason": projection.terminal_reason,
    }


def render_status(
    projection: InvocationProjection,
    *,
    root_input_digest: str | None = None,
    initial_tree_id: str | None = None,
    selected_test_families: tuple[str, ...] = (),
) -> StatusV1:
    return StatusV1.model_validate(
        project_status_fields(
            projection,
            root_input_digest=root_input_digest,
            initial_tree_id=initial_tree_id,
            selected_test_families=selected_test_families,
        )
    )


def _require_digest(value: str | None) -> str:
    del value
    raise ValueError("status projection requires root-input and initial-tree identities")


def _status_class(
    projection: InvocationProjection,
) -> Literal["running", "blocked", "interrupted", "stopped", "failed", "completed"]:
    if projection.pending_interrupt is not None:
        return "interrupted"
    return _PRODUCT_STATUS.get(projection.status, "running")


def _graph_status(graph: GraphInstanceRecord) -> GraphStatusV1:
    return GraphStatusV1(
        graph_instance_id=graph.graph_instance_id,
        graph_id=graph.graph_id,
        parent_graph_instance_id=graph.parent_graph_instance_id,
        state=_GRAPH_STATES.get(graph.status, "running"),
    )


def _node_status(activation: ActivationRecord) -> NodeStatusV1:
    last = activation.attempts[-1] if activation.attempts else None
    return NodeStatusV1(
        graph_instance_id=activation.graph_instance_id,
        node_id=activation.node_id,
        state=_node_state(activation, last),
        attempt=None if last is None else last.attempt,
        lease_state=None if last is None else last.status,
        failure_category=_failure_category(activation, last),
        activity_reference_digest=(
            None if last is None or last.activity is None else last.activity.reference_digest
        ),
    )


def _node_state(
    activation: ActivationRecord,
    last: AttemptRecord | None,
) -> Literal[
    "inactive",
    "ready",
    "running",
    "retrying",
    "succeeded",
    "failed",
    "stopped",
    "interrupted",
    "skipped",
]:
    if activation.status == "completed":
        return "succeeded"
    if activation.status == "failed":
        return "failed"
    if activation.status == "interrupted":
        return "interrupted"
    if activation.status == "stopped":
        return "stopped"
    if last is not None and last.attempt > 1 and last.status == "running":
        return "retrying"
    if last is not None and last.status == "running":
        return "running"
    return "running"


def _failure_category(activation: ActivationRecord, last: AttemptRecord | None) -> str | None:
    if last is not None and last.failure is not None:
        return last.failure.kind
    if activation.failure is not None:
        return activation.failure.kind
    return None


def _effect_status(effect: object) -> EffectStatusV1:
    receipt = getattr(effect, "receipt", None)
    receipt_digest = None if receipt is None else canonical_digest(cast(JSONValue, thaw_json(receipt)))
    return EffectStatusV1(
        effect_id=str(getattr(effect, "effect_id")),
        kind=str(getattr(effect, "kind")),
        state=str(getattr(effect, "status")),
        receipt_digest=receipt_digest,
    )


def _adapter_evidence(projection: InvocationProjection) -> tuple[AdapterEvidenceRefV1, ...]:
    refs: list[AdapterEvidenceRefV1] = []
    for activation in projection.activations:
        for attempt in activation.attempts:
            activity = attempt.activity
            if activity is None or activity.reference_digest is None:
                continue
            refs.append(
                AdapterEvidenceRefV1(
                    activation_id=activation.activation_id,
                    activity_id=activity.activity_id,
                    reference_digest=activity.reference_digest,
                    terminal_receipt_digest=activity.terminal_proof_digest,
                )
            )
    return tuple(refs)


def _pending_interrupt(projection: InvocationProjection) -> PendingInterruptStatusV1 | None:
    pending = projection.pending_interrupt
    if pending is None:
        return None
    activation = next(item for item in projection.activations if item.activation_id == pending.activation_id)
    return PendingInterruptStatusV1(
        node_id=activation.node_id,
        actions=pending.actions,
        reason_category=pending.reason,
    )


def _selected_families(projection: InvocationProjection) -> tuple[str, ...]:
    for graph in projection.graph_instances:
        if graph.parent_graph_instance_id is not None:
            continue
        payload = thaw_json(graph.input)
        if not isinstance(payload, Mapping):
            continue
        families = payload.get("selected_test_families")
        if isinstance(families, list | tuple):
            return tuple(str(item) for item in families)
    return ()


def _coverage_progress(projection: InvocationProjection) -> CoverageProgressV1 | None:
    for activation in projection.activations:
        payload = thaw_json(activation.output)
        if payload is None and activation.attempts:
            payload = thaw_json(activation.attempts[-1].output)
        if not isinstance(payload, Mapping):
            continue
        coverage = payload.get("coverage")
        if not isinstance(coverage, Mapping):
            continue
        if not {"measured", "rounds_used", "rounds_budget", "decision"}.issubset(coverage):
            continue
        decision = coverage["decision"]
        return CoverageProgressV1(
            round=int(coverage["rounds_used"]),
            maximum_rounds=int(coverage["rounds_budget"]),
            measured=coverage["measured"],
            decision="pass" if decision is True else ("continue" if decision is False else str(decision)),
        )
    return None
