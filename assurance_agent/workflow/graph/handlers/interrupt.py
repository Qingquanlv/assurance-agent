"""builtin interrupt handler：构建冻结的 ``InterruptProjection`` 与只读 artifact view。

interrupt ID 按结构成分（invocation/namespace/graph/node/task）经 canonical
SHA-256 派生——同一 task 重放必然得到同一 ID。``bind: audited_gate_read`` 的
audited hash 取自 checkpoint 对应 gate 的 audited reads 在 handler 所见
workspace view 中的当前内容；``healing.safety`` → ``fixer-safety-gate`` 的
checkpoint 别名镜像 v1 ``latest_valid_gate_decision`` 的特例。

handler 把 audited 文件物化到 ``.graph-runtime/views/<interrupt-id>/`` 并标记
只读；``graph_interrupted`` 仍由 scheduler 在 sibling settle 路径上发布，
resume 决策（``resolved_action``）由 GraphRuntime 写入投影。
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from assurance_agent.workflow.core.audit_scope import is_audited_gate_read
from assurance_agent.workflow.execution.tree_hash import sha256_file
from assurance_agent.workflow.graph.compiler import canonical_digest
from assurance_agent.workflow.graph.models import (
    CompiledWorkflow,
    ExecutableTask,
    InterruptProjection,
    RuntimeContext,
    TaskResult,
)
from assurance_agent.workflow.graph.task_runner import task_failure
from assurance_agent.workflow.graph.workspace import TaskWorkspace
from assurance_agent.workflow.orchestration.gates import (
    GateEvaluationContext,
    resolve_view_path,
)

# v1 特例：``healing.safety`` checkpoint 的 audited read 锚定 fixer-safety-gate。
_CHECKPOINT_GATE_ALIASES = {"healing.safety": "fixer-safety-gate"}


class InterruptHandler:
    def __init__(self, compiled: CompiledWorkflow) -> None:
        self._compiled = compiled

    def execute(
        self,
        task: ExecutableTask,
        workspace: TaskWorkspace,
        context: RuntimeContext,
    ) -> TaskResult:
        graph = self._compiled.graphs.get(task.graph_id)
        node = graph.nodes.get(task.node_id) if graph is not None else None
        interrupt = node.definition.interrupt if node is not None else None
        if interrupt is None:
            return task_failure(
                "contract",
                f"cannot resolve interrupt definition for task {task.task_id} "
                f"(graph '{task.graph_id}', node '{task.node_id}')",
            )
        interrupt_id = canonical_digest(
            {
                "invocation_id": task.invocation_id,
                "checkpoint_ns": task.checkpoint_ns,
                "graph_id": task.graph_id,
                "node_id": task.node_id,
                "task_id": task.task_id,
            }
        )
        eval_context = GateEvaluationContext(
            project_root=workspace.project_root,
            repo_root=workspace.repo_root,
            change_dir=workspace.change_dir,
            change_id=context.change_id,
            params={},
            state_values={},
            node_results={},
        )
        reads_sha256: dict[str, str] = {}
        sources: dict[str, Path] = {}
        for rel in self._audited_reads(interrupt.checkpoint):
            src = resolve_view_path(eval_context, rel)
            digest = sha256_file(src)
            if digest is not None:
                reads_sha256[rel] = digest
                sources[rel] = src
        artifact_view = self._materialize_view(
            context.change_dir, interrupt_id, sources
        )
        projection = InterruptProjection(
            interrupt_id=interrupt_id,
            checkpoint_ns=task.checkpoint_ns,
            node_id=task.node_id,
            checkpoint=interrupt.checkpoint,
            actions=tuple(interrupt.actions),
            audited_reads_sha256=reads_sha256,
            artifact_view=artifact_view,
            resolved_action=None,
        )
        return TaskResult(status="interrupted", interrupt=projection)

    def _audited_reads(self, checkpoint: str) -> tuple[str, ...]:
        gates = self._compiled.schema.gates
        gate_id = checkpoint if checkpoint in gates else _CHECKPOINT_GATE_ALIASES.get(checkpoint)
        gate = gates.get(gate_id) if gate_id is not None else None
        if gate is None:
            return ()
        return tuple(entry.path for entry in gate.reads if is_audited_gate_read(entry.path))

    @staticmethod
    def _materialize_view(
        change_dir: Path,
        interrupt_id: str,
        sources: dict[str, Path],
    ) -> str:
        rel = f".graph-runtime/views/{interrupt_id}"
        dest_root = change_dir / rel
        if dest_root.exists():
            shutil.rmtree(dest_root)
        dest_root.mkdir(parents=True, exist_ok=True)
        for path, src in sorted(sources.items()):
            target = dest_root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)
            os.chmod(target, 0o444)
        # 目录本身也尽量只读（父进程仍可在 resume 校验后替换整树）。
        try:
            os.chmod(dest_root, 0o555)
        except OSError:
            pass
        return rel


__all__ = ["InterruptHandler"]
