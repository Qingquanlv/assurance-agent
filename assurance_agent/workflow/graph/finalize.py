"""统一成功收尾：output 校验 → attached gate 求值 → 冻结 gate_report。

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
from pydantic import ValidationError

from assurance_agent.artifacts.registry import match_artifact
from assurance_agent.workflow.graph.models import (
    CompiledWorkflow,
    ExecutableTask,
    RuntimeContext,
    TaskResult,
)
from assurance_agent.workflow.graph.schema_v2 import NodeDef
from assurance_agent.workflow.graph.task_runner import task_failure
from assurance_agent.workflow.graph.workspace import TaskWorkspace, TreeStore, WorkspaceError
from assurance_agent.workflow.orchestration.gates import (
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


def _validate_registry_outputs(
    *,
    workspace: TaskWorkspace,
    outputs: tuple[str, ...],
) -> TaskResult | None:
    """Validate declared ``change:`` file outputs against their registry model.

    Gate expressions read artifact fields (e.g. a plan review's
    ``required_capabilities``) straight from the file, while freeze only checks
    the file exists — so a skill that omits a gate-critical field slips through
    and dead-ends at a terminal ``stop``. Running the artifact-registry pydantic
    model here converts that into an ``invalid_output`` task failure that the
    node's retry policy can recover from, surfacing the exact contract breach.

    Scope: single-file ``change:`` outputs whose registry spec is
    ``must_compat`` (the engine-enforced contract grade). ``versioned`` specs
    stay advisory and directory outputs (e.g. ``change:cases/``) are not
    expanded here.
    """
    for output in outputs:
        root, _, rest = output.partition(":")
        if root != "change" or not rest or rest.endswith("/"):
            continue
        spec = match_artifact(rest)
        if spec is None or spec.compat != "must_compat":
            continue
        try:
            raw = (workspace.change_dir / rest).read_text(encoding="utf-8")
        except OSError:
            continue  # existence is enforced by freeze; skip unreadable here
        is_yaml = rest.endswith((".yaml", ".yml"))
        try:
            data = yaml.safe_load(raw) if is_yaml else json.loads(raw)
        except (ValueError, yaml.YAMLError) as exc:
            kind = "YAML" if is_yaml else "JSON"
            return task_failure("invalid_output", f"output '{output}' is not valid {kind}: {exc}")
        try:
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
    state_values = _state_values_for_gate(workspace.change_dir, result)
    eval_context = GateEvaluationContext(
        project_root=workspace.project_root,
        repo_root=workspace.repo_root,
        change_dir=workspace.change_dir,
        change_id=context.change_id,
        params=context.params,
        state_values=state_values,
        node_results={},
    )
    try:
        report = check_gate_in_view(compiled.schema.gates, gate_id, eval_context)
    except GateError as exc:
        return task_failure("contract", str(exc))
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
    return result.model_copy(update={"gate_report": gate_report})


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
