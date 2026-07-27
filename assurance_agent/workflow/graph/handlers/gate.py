"""attached/builtin gate handler：在显式 artifact view 上做冻结 gate 裁决。

``builtin:gate`` 支持两种声明：

- ``with: {gate: <id>}``：命名 gate，经 ``check_gate_in_view`` 求值；
- ``with: {expression: ...}``：内联 DSL 表达式，artifact symbol 从 compiled
  graph 的 ``artifact_symbols`` 加载，缺读/解析失败按 MISSING fail closed。

两种形态的业务 status 都是 succeeded——verdict 本身不是 task 失败；route 只读
冻结的 ``gate_report``/``value``。projection 的 state/node 结局接线随 runtime
（Task 11）落地，当前以空映射求值（引用缺失一律 MISSING fail closed）。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from assurance_agent.workflow.core.events import LedgerIntegrityError, read_events_strict
from assurance_agent.workflow.graph.checkpoint import fold_invocation_events
from assurance_agent.workflow.graph.models import (
    CompiledWorkflow,
    ExecutableTask,
    RuntimeContext,
    TaskResult,
)
from assurance_agent.workflow.graph.node_history import build_node_results_for_gate
from assurance_agent.workflow.graph.task_runner import task_failure, task_with
from assurance_agent.workflow.graph.workspace import TaskWorkspace
from assurance_agent.workflow.orchestration.dsl import DslError, Scope, evaluate, parse_expression
from assurance_agent.workflow.orchestration.gates import (
    GateError,
    GateEvaluationContext,
    check_gate_in_view,
    expand_gate_read_template,
    resolve_view_path,
)


class GateHandler:
    def __init__(self, compiled: CompiledWorkflow) -> None:
        self._compiled = compiled

    def execute(
        self,
        task: ExecutableTask,
        workspace: TaskWorkspace,
        context: RuntimeContext,
    ) -> TaskResult:
        params = task_with(task)
        node_results = _node_results_for_gate(task, context.change_dir)
        eval_context = GateEvaluationContext(
            project_root=workspace.project_root,
            repo_root=workspace.repo_root,
            change_dir=workspace.change_dir,
            change_id=context.change_id,
            params=context.params,
            state_values={},
            node_results=node_results,
            audit_events_dir=context.change_dir,
        )
        gate_id = params.get("gate")
        if isinstance(gate_id, str):
            try:
                report = check_gate_in_view(self._compiled.schema.gates, gate_id, eval_context)
            except GateError as exc:
                return task_failure("contract", str(exc))
            value = report.verdict.value
            gate_report: dict[str, object] = {
                "gate_id": report.gate_id,
                "verdict": report.verdict.value,
                "matched_rule": report.matched_rule,
                "reason": report.reason,
                "reads_sha256": dict(report.reads_sha256),
                "value": value,
            }
            if report.details is not None:
                gate_report["details"] = dict(report.details)
            return TaskResult(status="succeeded", value=value, gate_report=gate_report)
        expression = params.get("expression")
        if isinstance(expression, str):
            return self._execute_expression(task, workspace, eval_context, expression)
        return task_failure(
            "contract",
            f"node '{task.node_id}' uses builtin:gate but declares neither gate nor with.expression",
        )

    def _execute_expression(
        self,
        task: ExecutableTask,
        workspace: TaskWorkspace,
        eval_context: GateEvaluationContext,
        expression: str,
    ) -> TaskResult:
        def node_result(node_id: str) -> object:
            result = eval_context.node_results.get(node_id)
            return result if isinstance(result, dict) else {}

        symbols = _load_artifact_symbols(self._compiled, task, workspace, params=eval_context.params)
        scope = Scope(
            {
                **symbols,
                "params": dict(eval_context.params),
                "state": dict(eval_context.state_values),
                "evidence": dict(task.resolved_evidence),
            },
            file_exists=lambda rel: resolve_view_path(eval_context, rel).exists(),
            node_result=node_result,
        )
        try:
            result = evaluate(parse_expression(expression), scope)
        except DslError as exc:
            return task_failure("contract", f"node '{task.node_id}' gate expression failed: {exc}")
        value = result is True  # MISSING/False 一律 fail closed 到 False
        return TaskResult(
            status="succeeded",
            value=value,
            gate_report={"expression": expression, "value": value},
        )


def _load_artifact_symbols(
    compiled: CompiledWorkflow,
    task: ExecutableTask,
    workspace: TaskWorkspace,
    *,
    params: Mapping[str, object],
) -> dict[str, object]:
    """按 compiled graph 的 artifact symbol 表从 task workspace 读 JSON；坏读按 MISSING 省略。"""
    graph = compiled.graphs.get(task.graph_id)
    if graph is None:
        return {}
    roots = {
        "change": workspace.change_dir,
        "project": workspace.project_root,
        "repo": workspace.repo_root,
    }
    variables: dict[str, object] = {}
    for symbol in sorted(graph.artifact_symbols):
        try:
            logical_path = expand_gate_read_template(graph.artifact_symbols[symbol], params=params)
        except GateError:
            continue
        root, _, rest = logical_path.partition(":")
        base = roots.get(root)
        if base is None:
            continue
        try:
            variables[symbol] = json.loads((base / rest).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
    return variables


def _node_results_for_gate(task: ExecutableTask, audit_events_dir: Path) -> dict[str, object]:
    try:
        projection = fold_invocation_events(task.invocation_id, read_events_strict(audit_events_dir))
        return build_node_results_for_gate(projection, graph_id=task.graph_id)
    except LedgerIntegrityError:
        return {}


__all__ = ["GateHandler"]
