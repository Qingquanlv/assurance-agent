from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
from pathlib import Path
from typing import Literal, cast

from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.frozen_json import thaw_json
from graph_engine.runtime.models import (
    ActivationRecord,
    AttemptRecord,
    GraphInstanceRecord,
    InvocationProjection,
)

from assurance_product.generated_merge import merge_generated
from assurance_product.models import (
    AdapterEvidenceRefV1,
    ApplyManifestFileV1,
    ApplyManifestV1,
    ApplyProjectionV1,
    ChangeProjectionV1,
    CoverageProgressV1,
    EffectStatusV1,
    GraphStatusV1,
    NodeStatusV1,
    PendingInterruptStatusV1,
    PublicationProjectionV1,
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
    change_id: str | None = None,
    apply_manifest_digest: str | None = None,
    apply_file_count: int = 0,
    publication_status: Literal["not_ready", "ready", "published", "drifted"] = "not_ready",
) -> dict[str, object]:
    del initial_tree_id
    if projection.status == "not_started" or projection.invocation_id is None:
        raise ValueError("cannot project status for an unstarted invocation")
    if projection.lock_digest is None or projection.entrypoint is None:
        raise ValueError("started projection requires complete invocation identity")
    families = selected_test_families or _selected_families(projection)
    status = _status_class(projection)
    resolved_change = change_id or _change_id(projection)
    change_state = _change_state(
        status,
        publication_status,
        entrypoint=projection.entrypoint or "",
    )
    return {
        "schema_version": "1",
        "invocation_id": projection.invocation_id,
        "lock_digest": projection.lock_digest,
        "root_input_digest": root_input_digest or _require_digest(root_input_digest),
        "status": status,
        "entrypoint": projection.entrypoint,
        "graph_hierarchy": tuple(_graph_status(item) for item in projection.graph_instances),
        "node_states": tuple(_node_status(item) for item in projection.activations),
        "selected_test_families": families,
        "coverage_progress": _coverage_progress(projection),
        "durable_effects": tuple(_effect_status(item) for item in projection.effects),
        "adapter_evidence": tuple(_adapter_evidence(projection)),
        "pending_interrupt": _pending_interrupt(projection),
        "terminal_reason": projection.terminal_reason,
        "change": ChangeProjectionV1(change_id=resolved_change, state=change_state),
        "apply": ApplyProjectionV1(manifest_digest=apply_manifest_digest, file_count=apply_file_count),
        "publication": PublicationProjectionV1(status=publication_status),
    }


def render_status(
    projection: InvocationProjection,
    *,
    root_input_digest: str | None = None,
    initial_tree_id: str | None = None,
    selected_test_families: tuple[str, ...] = (),
    change_id: str | None = None,
    apply_manifest_digest: str | None = None,
    apply_file_count: int = 0,
    publication_status: Literal["not_ready", "ready", "published", "drifted"] = "not_ready",
) -> StatusV1:
    return StatusV1.model_validate(
        project_status_fields(
            projection,
            root_input_digest=root_input_digest,
            initial_tree_id=initial_tree_id,
            selected_test_families=selected_test_families,
            change_id=change_id,
            apply_manifest_digest=apply_manifest_digest,
            apply_file_count=apply_file_count,
            publication_status=publication_status,
        )
    )


def finalize_achieved(
    project_root: Path,
    change_id: str,
    families: tuple[str, ...],
    *,
    invocation: Mapping[str, object] | StatusV1,
) -> StatusV1:
    project = Path(project_root)
    _require_terminal_full_success(invocation)
    merged = merge_generated(project, change_id, families)
    _require_execution_gate(project, change_id)
    _require_quality_gate(project, change_id)
    manifest = _apply_manifest(project, change_id, merged)
    status = _achieved_status(invocation, change_id, manifest)
    change_root = project / "qa" / "changes" / change_id
    _write_canonical_json(change_root / "apply-manifest.json", manifest.model_dump(mode="json"))
    _write_canonical_json(change_root / "status.json", status.model_dump(mode="json"))
    return status


def _require_terminal_full_success(invocation: Mapping[str, object] | StatusV1) -> None:
    payload = invocation.model_dump(mode="json") if isinstance(invocation, StatusV1) else dict(invocation)
    if payload.get("entrypoint") != "full":
        raise ValueError("achieved requires the full entrypoint")
    if payload.get("status") != "completed":
        raise ValueError("achieved requires terminal workflow success")
    if payload.get("pending_interrupt") is not None:
        raise ValueError("achieved requires no pending interrupt")


def _change_state(
    status: Literal["running", "blocked", "interrupted", "stopped", "failed", "completed"],
    publication_status: Literal["not_ready", "ready", "published", "drifted"],
    *,
    entrypoint: str = "",
) -> Literal["running", "blocked", "interrupted", "stopped", "failed", "achieved"]:
    if status == "completed" and (entrypoint == "full" or publication_status in {"ready", "published"}):
        return "achieved"
    if status == "blocked":
        return "blocked"
    if status == "interrupted":
        return "interrupted"
    if status == "stopped":
        return "stopped"
    if status == "failed":
        return "failed"
    return "running"


def _require_digest(value: str | None) -> str:
    del value
    raise ValueError("status projection requires a root-input digest")


def _change_id(projection: InvocationProjection) -> str:
    for graph in projection.graph_instances:
        if graph.parent_graph_instance_id is not None:
            continue
        payload = thaw_json(graph.input)
        if isinstance(payload, Mapping):
            change_id = payload.get("change_id")
            if isinstance(change_id, str) and change_id:
                return change_id
    raise ValueError("status projection requires a change_id")


def _require_execution_gate(project: Path, change_id: str) -> None:
    path = project / "qa" / "changes" / change_id / "execution" / "execute-result.json"
    payload = _read_json_object(path, "execution evidence")
    status = payload.get("status")
    if status != "passed":
        raise ValueError(f"execution gate failed: {status!r}")


def _require_quality_gate(project: Path, change_id: str) -> None:
    inspect_path = project / "qa" / "changes" / change_id / "inspect" / "inspection.json"
    report_path = project / "qa" / "changes" / change_id / "report" / "report.md"
    payload = _read_json_object(inspect_path, "quality evidence")
    coverage = payload.get("coverage")
    if not isinstance(coverage, Mapping) or coverage.get("decision") is not True:
        raise ValueError("quality gate failed")
    if not report_path.is_file() or report_path.is_symlink():
        raise ValueError("quality gate failed: report is missing")


def _apply_manifest(project: Path, change_id: str, merged: object) -> ApplyManifestV1:
    files = []
    for item in getattr(merged, "files"):
        target = project.joinpath(*str(item.target_path).split("/"))
        baseline = None
        if target.is_file() and not target.is_symlink() and target.stat().st_nlink == 1:
            baseline = f"sha256:{hashlib.sha256(target.read_bytes()).hexdigest()}"
        files.append(
            ApplyManifestFileV1(
                target_path=item.target_path,
                source_path=item.staged_path,
                source_sha256=item.sha256,
                baseline_sha256=baseline,
                mode=item.mode,
                operation=item.operation,
            )
        )
    ordered = tuple(sorted(files, key=lambda item: item.target_path))
    return ApplyManifestV1(
        schema_version="1",
        change_id=change_id,
        digest=str(getattr(merged, "digest")),
        files=ordered,
    )


def _achieved_status(
    invocation: Mapping[str, object] | StatusV1,
    change_id: str,
    manifest: ApplyManifestV1,
) -> StatusV1:
    payload = invocation.model_dump(mode="json") if isinstance(invocation, StatusV1) else dict(invocation)
    payload["change"] = {"change_id": change_id, "state": "achieved"}
    payload["apply"] = {"manifest_digest": manifest.digest, "file_count": len(manifest.files)}
    payload["publication"] = {"status": "ready"}
    return StatusV1.model_validate(payload)


def _read_json_object(path: Path, label: str) -> Mapping[str, object]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} is missing")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} is invalid") from error
    if not isinstance(payload, Mapping):
        raise ValueError(f"{label} is invalid")
    return payload


def _write_canonical_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


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
