"""统一成功收尾：output 校验 → blob 摄入 → attached gate 求值 → 冻结 gate_report。

handler 只负责业务副作用；本模块在 NodeRunner 成功路径上补齐 NodeDef.outputs /
NodeDef.gate 合同，使 operation / skill / subgraph 与 builtin:gate 共享同一套
route 可读的 ``gate_report``。

Candidate precommit validation (D14) runs later in the scheduler, after this
module's freeze/ingest and before ``task_attempt_succeeded`` is appended.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError

from assurance_kernel.artifacts.paths import WORKFLOW_STATE_REL, existing_with_alias
from assurance_kernel.artifacts.registry import ArtifactSpec, load_registered_artifact, match_artifact
from assurance_kernel.workflow.core.events import LedgerIntegrityError, read_events_strict
from assurance_kernel.workflow.graph.checkpoint import fold_invocation_events
from assurance_kernel.workflow.graph.codegen_manifest import (
    CodegenCompletionReceipt,
    CodegenManifestError,
    complete_codegen_manifest,
    verify_frozen_codegen_completion,
)
from assurance_kernel.workflow.graph.frozen_output import (
    FrozenOutput,
    candidate_value_map,
    frozen_outputs_wire,
)
from assurance_kernel.workflow.graph.ingest import ingest_from_write_set
from assurance_kernel.workflow.graph.models import (
    CompiledWorkflow,
    ExecutableTask,
    GraphProjection,
    RuntimeContext,
    TaskResult,
)
from assurance_kernel.workflow.graph.node_history import build_node_results_for_gate
from assurance_kernel.workflow.graph.reviewer_plan_checks import (
    PlanChecksCompletionError,
    complete_reviewer_plan_checks,
)
from assurance_kernel.workflow.graph.schema_v2 import NodeDef
from assurance_kernel.workflow.graph.selected_wave import derive_child_invocation_id
from assurance_kernel.workflow.graph.subgraph_exports import SubgraphExportError, apply_subgraph_exports
from assurance_kernel.workflow.graph.task_runner import task_failure
from assurance_kernel.workflow.graph.workspace import TaskWorkspace, TreeStore, WorkspaceError
from assurance_kernel.workflow.orchestration.gates import (
    FrozenGateReport,
    GateError,
    GateEvaluationContext,
    check_gate_in_view,
)


def finalize_task_result(
    *,
    compiled: CompiledWorkflow,
    store: TreeStore,
    task: ExecutableTask,
    result: TaskResult,
    workspace: TaskWorkspace,
    context: RuntimeContext,
    ingest_catalog: object | None = None,
    model_map: object | None = None,
) -> TaskResult:
    """在 handler 返回 succeeded/stopped 后执行 output + attached-gate 合同。"""
    if result.status not in ("succeeded", "stopped"):
        return result

    node_def = _node_def(compiled, task)
    outputs = _resolve_outputs(task, node_def)

    if result.status == "succeeded":
        codegen_receipt: CodegenCompletionReceipt | None = None
        if result.write_set_id is None:
            try:
                codegen_receipt = complete_codegen_manifest(
                    task=task,
                    workspace=workspace,
                    context=context,
                )
            except CodegenManifestError as exc:
                return task_failure("invalid_output", str(exc))
            try:
                complete_reviewer_plan_checks(
                    task=task,
                    workspace=workspace,
                    context=context,
                )
            except PlanChecksCompletionError as exc:
                return task_failure("invalid_output", str(exc))
        result = _ensure_outputs_frozen(
            store=store,
            task=task,
            result=result,
            workspace=workspace,
            outputs=outputs,
        )
        if result.status != "succeeded":
            return result
        if codegen_receipt is not None:
            assert result.write_set_id is not None
            try:
                verify_frozen_codegen_completion(
                    store=store,
                    write_set_id=result.write_set_id,
                    receipt=codegen_receipt,
                )
            except CodegenManifestError as exc:
                return task_failure("invalid_output", str(exc))
        result = _ingest_frozen_outputs(
            store=store,
            task=task,
            result=result,
            outputs=outputs,
            ingest_catalog=ingest_catalog,
            model_map=model_map,
        )
        if result.status != "succeeded":
            return result
        result = _apply_subgraph_exports(
            compiled=compiled,
            task=task,
            result=result,
            context=context,
        )
        if result.status != "succeeded":
            return result
        written_outputs: tuple[str, ...] = ()
        if result.write_set_id is not None:
            try:
                write_set = store.load_write_set(result.write_set_id)
            except WorkspaceError as exc:
                return task_failure("invalid_output", str(exc))
            written_outputs = tuple(
                entry.logical_path for entry in write_set.entries if entry.operation != "delete"
            )
        registry_invalid = _validate_registry_outputs(
            workspace=workspace,
            outputs=outputs,
            written_outputs=written_outputs,
        )
        if registry_invalid is not None:
            return registry_invalid

    if result.status == "succeeded" and node_def is not None and node_def.gate and result.gate_report is None:
        result = _attach_gate_report(
            compiled=compiled,
            task=task,
            result=result,
            workspace=workspace,
            context=context,
            gate_id=node_def.gate,
        )
    return result


def _node_def(compiled: CompiledWorkflow, task: ExecutableTask) -> NodeDef | None:
    graph = compiled.graphs.get(task.graph_id)
    if graph is None:
        return None
    node = graph.nodes.get(task.node_id)
    return node.definition if node is not None else None


def _resolve_outputs(task: ExecutableTask, node_def: NodeDef | None) -> tuple[str, ...]:
    payload = task.input
    if isinstance(payload, Mapping):
        expanded = payload.get("outputs")
        if isinstance(expanded, list) and all(isinstance(item, str) for item in expanded):
            return tuple(expanded)
    if node_def is not None:
        return tuple(node_def.outputs)
    return ()


def _ensure_outputs_frozen(
    *,
    store: TreeStore,
    task: ExecutableTask,
    result: TaskResult,
    workspace: TaskWorkspace,
    outputs: tuple[str, ...],
) -> TaskResult:
    if result.write_set_id is not None:
        return result
    if not outputs and not task.resources.writes and not task.resources.authorization_writes:
        return result
    try:
        write_set = store.freeze_write_set(workspace, claims=task.resources, outputs=outputs)
    except WorkspaceError as exc:
        kind = "invalid_output" if "output" in str(exc).lower() else "forbidden_write"
        return task_failure(kind, str(exc))
    return result.model_copy(
        update={
            "write_set_id": write_set.write_set_id,
            "outputs_sha256": dict(write_set.outputs_sha256),
        }
    )


def _ingest_frozen_outputs(
    *,
    store: TreeStore,
    task: ExecutableTask,
    result: TaskResult,
    outputs: tuple[str, ...],
    ingest_catalog: object | None = None,
    model_map: object | None = None,
) -> TaskResult:
    """Blob 摄入 → ``frozen_outputs`` wire map + ``candidate_outputs`` value map。"""
    frozen: dict[str, FrozenOutput] = {}
    if result.write_set_id is not None and outputs:
        try:
            frozen = ingest_from_write_set(
                store,
                write_set_id=result.write_set_id,
                output_paths=outputs,
                catalog=ingest_catalog,  # type: ignore[arg-type]
                model_map=model_map,  # type: ignore[arg-type]
            )
        except (ValueError, WorkspaceError) as exc:
            return task_failure("invalid_output", str(exc))
    wire = frozen_outputs_wire(frozen)
    candidate = candidate_value_map(frozen)
    return result.model_copy(update={"frozen_outputs": wire, "candidate_outputs": candidate})


def _apply_subgraph_exports(
    *,
    compiled: CompiledWorkflow,
    task: ExecutableTask,
    result: TaskResult,
    context: RuntimeContext,
) -> TaskResult:
    if not task.target.startswith("graph:"):
        return result
    graph = compiled.graphs.get(task.graph_id)
    if graph is None:
        return result
    node = graph.nodes.get(task.node_id)
    if node is None or not node.exports:
        return result
    child_graph_id = task.target.split(":", 1)[1]
    child_invocation_id = derive_child_invocation_id(task, child_graph_id)
    try:
        # Nested subgraph events are written to the single per-change ledger
        # (context.change_dir), not the parent task's private workspace — the
        # subgraph runs in-process and commits graph_invocation_started there
        # before this parent task finalizes. Folding workspace.change_dir would
        # miss the child's start event and fail closed with a spurious
        # LedgerIntegrityError (mirrors scheduler's context.change_dir folds).
        child_projection = fold_invocation_events(
            child_invocation_id,
            read_events_strict(context.change_dir),
        )
        exported = apply_subgraph_exports(
            child_projection=child_projection,
            child_graph_id=child_graph_id,
            export_defs=node.exports,
        )
    except (LedgerIntegrityError, SubgraphExportError) as exc:
        return task_failure("invalid_output", str(exc))
    wire = dict(result.frozen_outputs)
    wire.update(frozen_outputs_wire(exported))
    candidate = dict(result.candidate_outputs)
    candidate.update(candidate_value_map(exported))
    return result.model_copy(update={"frozen_outputs": wire, "candidate_outputs": candidate})


def _authoring_obligations_model(spec: ArtifactSpec) -> type[BaseModel] | None:
    """The authoring model that still constrains this file once frozen, if any.

    Two unrelated shapes share the ``authoring_model`` slot. A draft stage that
    the runtime later completes is already past authoring by the time finalize
    reads the file. That relationship is expressed either by canonical-model
    inheritance or by ``runtime_completes_authoring`` when nested field narrowing
    makes inheritance type-unsafe. An authoring contract that only adds
    obligations to what a skill must hand in (review ``source_verification``)
    never gets completed, so the frozen file is the authored one and must satisfy
    it.
    """
    authoring = spec.authoring_model
    if authoring is None or spec.runtime_completes_authoring or issubclass(spec.model, authoring):
        return None
    return authoring


def _validate_registry_outputs(
    *,
    workspace: TaskWorkspace,
    outputs: tuple[str, ...],
    written_outputs: tuple[str, ...] = (),
) -> TaskResult | None:
    """Validate authored project/change artifacts against their registry model.

    Declared directories are expanded, and the frozen write-set paths are added
    so an agent cannot hide an optional registry artifact behind a broader
    authorization such as ``change:plans/**``.
    """
    candidates: list[str] = []
    for output in (*outputs, *written_outputs):
        root, _, rest = output.partition(":")
        if root not in {"change", "project"} or not rest:
            continue
        if rest.endswith("/"):
            output_root = workspace.change_dir if root == "change" else workspace.project_root
            directory = output_root / rest
            try:
                descendants = sorted(path for path in directory.rglob("*") if path.is_file())
            except OSError as exc:
                return task_failure(
                    "invalid_output",
                    f"output '{output}' could not be expanded for schema validation: {exc}",
                )
            candidates.extend(f"{root}:{path.relative_to(output_root).as_posix()}" for path in descendants)
            continue
        candidates.append(output)

    for output in dict.fromkeys(candidates):
        root, _, rest = output.partition(":")
        spec = match_artifact(rest)
        if spec is None or spec.compat != "must_compat":
            continue
        try:
            output_root = workspace.change_dir if root == "change" else workspace.project_root
            raw = (output_root / rest).read_text(encoding="utf-8")
        except OSError as exc:
            return task_failure(
                "invalid_output",
                f"output '{output}' could not be read for schema validation: {exc}",
            )
        try:
            data = load_registered_artifact(rest, raw)
        except (ValueError, yaml.YAMLError) as exc:
            kind = spec.wire.upper()
            return task_failure("invalid_output", f"output '{output}' is not valid {kind}: {exc}")
        try:
            authoring = _authoring_obligations_model(spec)
            if authoring is not None:
                authoring.model_validate(data)
            spec.model.model_validate(data)
        except ValidationError as exc:
            return task_failure(
                "invalid_output",
                f"output '{output}' failed {spec.artifact_type} schema validation: {exc}",
            )
    return None


def _attach_gate_report(
    *,
    compiled: CompiledWorkflow,
    task: ExecutableTask,
    result: TaskResult,
    workspace: TaskWorkspace,
    context: RuntimeContext,
    gate_id: str,
) -> TaskResult:
    node_results: dict[str, object] = {}
    projection: GraphProjection | None = None
    try:
        projection = fold_invocation_events(task.invocation_id, read_events_strict(workspace.change_dir))
        node_results = build_node_results_for_gate(projection, graph_id=task.graph_id)
    except LedgerIntegrityError:
        projection = None
        node_results = {}
    state_values = _state_values_for_gate(workspace.change_dir, result, projection=projection)
    own_payload: dict[str, object] = {"status": "succeeded"}
    if result.candidate_outputs:
        own_payload["outputs"] = dict(result.candidate_outputs)
    node_results = {**node_results, task.node_id: own_payload}
    overrides = _candidate_artifact_overrides(result.candidate_outputs, task, compiled)
    eval_context = GateEvaluationContext(
        project_root=workspace.project_root,
        repo_root=workspace.repo_root,
        change_dir=workspace.change_dir,
        change_id=context.change_id,
        params=context.params,
        state_values=state_values,
        node_results=node_results,
        artifact_overrides=overrides,
        audit_events_dir=context.change_dir,
        checkpoint_ns=task.checkpoint_ns,
        host_project_root=context.resolved_host_root,
    )
    try:
        candidate_report = check_gate_in_view(compiled.schema.gates, gate_id, eval_context)
        disk_report = check_gate_in_view(
            compiled.schema.gates,
            gate_id,
            GateEvaluationContext(
                project_root=workspace.project_root,
                repo_root=workspace.repo_root,
                change_dir=workspace.change_dir,
                change_id=context.change_id,
                params=context.params,
                state_values=state_values,
                node_results=node_results,
                audit_events_dir=context.change_dir,
                checkpoint_ns=task.checkpoint_ns,
                host_project_root=context.resolved_host_root,
            ),
        )
    except GateError as exc:
        return task_failure("contract", str(exc))
    drift = _gate_reports_differ(candidate_report, disk_report)
    if drift is not None:
        return task_failure("contract", f"gate dual-run mismatch: {drift}")
    return result.model_copy(update={"gate_report": _gate_report_dict(candidate_report)})


def _candidate_artifact_overrides(
    candidate_outputs: Mapping[str, object],
    task: ExecutableTask,
    compiled: CompiledWorkflow,
) -> dict[str, object]:
    """Map gate read paths to in-memory candidate values for this task's outputs."""
    if not candidate_outputs:
        return {}
    graph = compiled.graphs.get(task.graph_id)
    if graph is None:
        return {}
    node = graph.nodes.get(task.node_id)
    if node is None:
        return {}
    overrides: dict[str, object] = {}
    for output in node.definition.outputs:
        root, _, rest = output.partition(":")
        if root != "change" or not rest or rest.endswith("/"):
            continue
        symbol = rest.replace("/", "_").replace(".", "_")
        value = candidate_outputs.get(symbol)
        if value is not None:
            overrides[rest] = value
    return overrides


def _gate_reports_differ(candidate: FrozenGateReport, disk: FrozenGateReport) -> str | None:
    if candidate.verdict != disk.verdict:
        return f"verdict {candidate.verdict.value} vs {disk.verdict.value}"
    if candidate.matched_rule != disk.matched_rule:
        return f"matched_rule {candidate.matched_rule!r} vs {disk.matched_rule!r}"
    if candidate.reason != disk.reason:
        return f"reason {candidate.reason!r} vs {disk.reason!r}"
    return None


def _gate_report_dict(report: FrozenGateReport) -> dict[str, object]:
    gate_report: dict[str, object] = {
        "gate_id": report.gate_id,
        "verdict": report.verdict.value,
        "matched_rule": report.matched_rule,
        "reason": report.reason,
        "reads_sha256": dict(report.reads_sha256),
        "value": report.verdict.value,
    }
    if report.details is not None:
        gate_report["details"] = dict(report.details)
    return gate_report


def _state_values_for_gate(
    change_dir: Path,
    result: TaskResult,
    *,
    projection: GraphProjection | None = None,
) -> dict[str, Any]:
    """Folded ``projection.state_values`` when present; YAML only with no projection."""
    if projection is not None:
        state: dict[str, Any] = dict(projection.state_values)
    else:
        state = {}
        path = existing_with_alias(change_dir / WORKFLOW_STATE_REL)
        if path is not None:
            try:
                text = path.read_text(encoding="utf-8")
                rel = path.name
                raw = load_registered_artifact(rel, text)
                raw = raw or {}
            except (OSError, yaml.YAMLError, ValueError):
                raw = {}
            if isinstance(raw, dict):
                state = {
                    key: value
                    for key, value in raw.items()
                    if key not in {"_integrity", "schema_version"} and not str(key).startswith("_")
                }
    updates = result.state_updates
    if updates:
        state = _deep_merge(state, dict(updates))
    return state


def _deep_merge(base: dict[str, Any], overlay: Mapping[str, object]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, Mapping):
            merged[key] = _deep_merge(existing, value)  # type: ignore[arg-type]
        else:
            merged[key] = value
    return merged


__all__ = ["finalize_task_result"]
