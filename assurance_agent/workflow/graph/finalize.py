"""统一成功收尾：output 校验 → blob 摄入 → attached gate 求值 → 冻结 gate_report。

handler 只负责业务副作用；本模块在 NodeRunner 成功路径上补齐 NodeDef.outputs /
NodeDef.gate 合同，使 operation / skill / subgraph 与 builtin:gate 共享同一套
route 可读的 ``gate_report``。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError

from assurance_agent.artifacts.registry import ArtifactSpec, match_artifact
from assurance_agent.artifacts.models.issues import IssueAnalysisStatus, IssueCandidateDocument
from assurance_agent.workflow.core.events import LedgerIntegrityError, read_events_strict
from assurance_agent.workflow.graph.checkpoint import fold_invocation_events
from assurance_agent.workflow.graph.frozen_output import (
    FrozenOutput,
    candidate_value_map,
    frozen_outputs_wire,
)
from assurance_agent.workflow.graph.subgraph_exports import SubgraphExportError, apply_subgraph_exports
from assurance_agent.workflow.graph.ingest import ingest_from_write_set
from assurance_agent.workflow.graph.models import (
    CompiledWorkflow,
    ExecutableTask,
    RuntimeContext,
    TaskResult,
)
from assurance_agent.workflow.graph.node_history import build_node_results_for_gate
from assurance_agent.workflow.graph.schema_v2 import NodeDef
from assurance_agent.workflow.graph.compiler import canonical_digest
from assurance_agent.workflow.graph.task_runner import task_failure
from assurance_agent.workflow.graph.workspace import TaskWorkspace, TreeStore, WorkspaceError
from assurance_agent.workflow.orchestration.gates import (
    FrozenGateReport,
    GateError,
    GateEvaluationContext,
    check_gate_in_view,
)
from assurance_agent.workflow.issues.identity import candidate_document_digest


def finalize_task_result(
    *,
    compiled: CompiledWorkflow,
    store: TreeStore,
    task: ExecutableTask,
    result: TaskResult,
    workspace: TaskWorkspace,
    context: RuntimeContext,
) -> TaskResult:
    """在 handler 返回 succeeded/stopped 后执行 output + attached-gate 合同。"""
    if result.status not in ("succeeded", "stopped"):
        return result

    node_def = _node_def(compiled, task)
    outputs = _resolve_outputs(task, node_def)

    if result.status == "succeeded":
        result = _ensure_outputs_frozen(
            store=store,
            task=task,
            result=result,
            workspace=workspace,
            outputs=outputs,
        )
        if result.status != "succeeded":
            return result
        result = _ingest_frozen_outputs(
            store=store,
            task=task,
            result=result,
            outputs=outputs,
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
        invalid = _validate_registry_outputs(workspace=workspace, outputs=outputs)
        if invalid is not None:
            return invalid

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
) -> TaskResult:
    """Blob 摄入 → ``frozen_outputs`` wire map + ``candidate_outputs`` value map。"""
    frozen: dict[str, FrozenOutput] = {}
    if result.write_set_id is not None and outputs:
        try:
            frozen = ingest_from_write_set(
                store,
                write_set_id=result.write_set_id,
                output_paths=outputs,
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
    child_invocation_id = canonical_digest({"parent_task_id": task.task_id, "graph_id": child_graph_id})
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
    the runtime later completes (canonical = draft + engine-owned fields, so the
    canonical model subclasses the draft) is already past authoring by the time
    finalize reads the file. An authoring contract that only adds obligations to
    what a skill must hand in (review ``source_verification``) never gets
    completed, so the frozen file is the authored one and must satisfy it.
    """
    authoring = spec.authoring_model
    if authoring is None or issubclass(spec.model, authoring):
        return None
    return authoring


def _validate_registry_outputs(
    *,
    workspace: TaskWorkspace,
    outputs: tuple[str, ...],
) -> TaskResult | None:
    """Validate declared project/change file outputs against their registry model."""
    validated: dict[str, Any] = {}
    authored: dict[str, Any] = {}
    for output in outputs:
        root, _, rest = output.partition(":")
        if root not in {"change", "project"} or not rest or rest.endswith("/"):
            continue
        spec = match_artifact(rest)
        if spec is None or spec.compat != "must_compat":
            continue
        try:
            output_root = workspace.change_dir if root == "change" else workspace.project_root
            raw = (output_root / rest).read_text(encoding="utf-8")
        except OSError:
            continue
        is_yaml = rest.endswith((".yaml", ".yml"))
        try:
            data = yaml.safe_load(raw) if is_yaml else json.loads(raw)
        except (ValueError, yaml.YAMLError) as exc:
            kind = "YAML" if is_yaml else "JSON"
            return task_failure("invalid_output", f"output '{output}' is not valid {kind}: {exc}")
        try:
            authored[output] = data
            authoring = _authoring_obligations_model(spec)
            if authoring is not None:
                authoring.model_validate(data)
            validated[output] = spec.model.model_validate(data)
        except ValidationError as exc:
            return task_failure(
                "invalid_output",
                f"output '{output}' failed {spec.artifact_type} schema validation: {exc}",
            )
    candidate = validated.get("change:inspect/issue-candidates.json")
    analysis_status = validated.get("change:inspect/issue-analysis-status.json")
    if isinstance(candidate, IssueCandidateDocument) and isinstance(analysis_status, IssueAnalysisStatus):
        expected = candidate_document_digest(authored["change:inspect/issue-candidates.json"])
        if analysis_status.candidate_digest != expected:
            return task_failure(
                "invalid_output",
                "output 'change:inspect/issue-analysis-status.json' candidate_digest "
                f"must equal canonical issue-candidates digest {expected!r}",
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
    state_values = _state_values_for_gate(workspace.change_dir, result)
    node_results: dict[str, object] = {}
    try:
        projection = fold_invocation_events(task.invocation_id, read_events_strict(workspace.change_dir))
        node_results = build_node_results_for_gate(projection, graph_id=task.graph_id)
    except LedgerIntegrityError:
        node_results = {}
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


def _state_values_for_gate(change_dir: Path, result: TaskResult) -> dict[str, Any]:
    """优先读 workspace 内 workflow-state.yaml，再叠本 task 的 state_updates。"""
    state: dict[str, Any] = {}
    path = change_dir / "workflow-state.yaml"
    if path.is_file():
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
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
