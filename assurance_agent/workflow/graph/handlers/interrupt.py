"""builtin interrupt handler：构建冻结的 ``InterruptProjection`` 与只读 artifact view。

interrupt ID 按结构成分（invocation/namespace/graph/node/task）经 canonical
SHA-256 派生——同一 task 重放必然得到同一 ID。``bind: audited_gate_read`` 的
audited hash 取自 checkpoint 对应 gate 的 audited reads 在 handler 所见
workspace view 中的当前内容；``healing.safety`` → ``fixer-safety-gate`` 的
checkpoint 别名镜像 v1 ``latest_valid_gate_decision`` 的特例。

handler 把 audited 文件物化到 ``.graph-runtime/views/<interrupt-id>/`` 并标记
只读；带 ``manual_revision`` 的 schema interrupt 另物化 bounded revision view。
``graph_interrupted`` 仍由 scheduler 在 sibling settle 路径上发布，resume
决策（``resolved_action``）由 GraphRuntime 写入投影。
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from assurance_agent.workflow.core.audit_scope import is_audited_gate_read
from assurance_agent.workflow.core.events import LedgerIntegrityError, read_events_strict
from assurance_agent.workflow.execution.tree_hash import sha256_file
from assurance_agent.workflow.graph.compiler import canonical_digest
from assurance_agent.workflow.graph.manual_revision import (
    RevisionPathBaseline,
    RevisionViewBinding,
    materialize_revision_view,
    resolve_gate_evidence_epoch,
)
from assurance_agent.workflow.graph.models import (
    CompiledWorkflow,
    ExecutableTask,
    InterruptProjection,
    RuntimeContext,
    TaskResult,
)
from assurance_agent.workflow.graph.task_runner import task_failure
from assurance_agent.workflow.graph.workspace import TaskWorkspace, TreeStore
from assurance_agent.workflow.orchestration.gates import (
    CHECKPOINT_GATE_ALIASES,
    GateEvaluationContext,
    resolve_checkpoint_gate_id,
    resolve_view_path,
)


class InterruptHandler:
    def __init__(self, compiled: CompiledWorkflow, object_store: TreeStore | None = None) -> None:
        self._compiled = compiled
        self._object_store = object_store

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
        artifact_view = self._materialize_view(context.change_dir, interrupt_id, sources)

        source_gate_attempt_id: str | None = None
        source_gate_tree_id: str | None = None
        revision_owner_invocation_id: str | None = None
        revision_base_tree_id: str | None = None
        revision_view: str | None = None
        revision_paths: tuple[str, ...] | None = None
        revision_before_sha256: dict[str, str] | None = None

        events: list[dict[str, object]] = []
        try:
            events = read_events_strict(context.change_dir)
        except (LedgerIntegrityError, OSError):
            events = []

        epoch = None
        gate_ids = frozenset(self._compiled.schema.gates)
        if interrupt.manual_revision is not None or resolve_checkpoint_gate_id(
            interrupt.checkpoint, gate_ids
        ):
            epoch = resolve_gate_evidence_epoch(
                events=events,
                invocation_id=task.invocation_id,
                checkpoint=interrupt.checkpoint,
                gate_ids=gate_ids,
                checkpoint_gate_aliases=CHECKPOINT_GATE_ALIASES,
            )
        if epoch is not None:
            source_gate_attempt_id = epoch.source_gate_attempt_id
            source_gate_tree_id = epoch.source_gate_tree_id
        elif interrupt.manual_revision is None and resolve_checkpoint_gate_id(interrupt.checkpoint, gate_ids):
            # Gate-backed interrupt without a prior successful attempt stays pairless.
            pass

        if interrupt.manual_revision is not None:
            if self._object_store is None:
                return task_failure(
                    "contract",
                    f"interrupt {interrupt_id} declares manual_revision but object store is unavailable",
                )
            if epoch is None:
                # Manual revision requires a resolvable gate evidence epoch.
                source_gate_attempt_id = None
                source_gate_tree_id = None
            committed_binding = self._committed_revision_binding(events, interrupt_id)
            binding = materialize_revision_view(
                change_dir=context.change_dir,
                store=self._object_store,
                interrupt_id=interrupt_id,
                owner_invocation_id=task.invocation_id,
                base_tree_id=workspace.base_tree_id,
                logical_paths=tuple(interrupt.manual_revision.paths),
                committed_binding=committed_binding,
            )
            revision_owner_invocation_id = binding.owner_invocation_id
            revision_base_tree_id = binding.base_tree_id
            revision_view = binding.view_relpath
            revision_paths = binding.logical_paths
            revision_before_sha256 = {item.logical_path: item.sha256 for item in binding.baseline}

        projection = InterruptProjection(
            interrupt_id=interrupt_id,
            checkpoint_ns=task.checkpoint_ns,
            node_id=task.node_id,
            checkpoint=interrupt.checkpoint,
            actions=tuple(interrupt.actions),
            audited_reads_sha256=reads_sha256,
            artifact_view=artifact_view,
            resolved_action=None,
            revision_owner_invocation_id=revision_owner_invocation_id,
            revision_base_tree_id=revision_base_tree_id,
            revision_view=revision_view,
            revision_paths=revision_paths,
            revision_before_sha256=revision_before_sha256,
            source_gate_attempt_id=source_gate_attempt_id,
            source_gate_tree_id=source_gate_tree_id,
        )
        return TaskResult(status="interrupted", interrupt=projection)

    def _audited_reads(self, checkpoint: str) -> tuple[str, ...]:
        gates = self._compiled.schema.gates
        gate_id = resolve_checkpoint_gate_id(checkpoint, frozenset(gates))
        gate = gates.get(gate_id) if gate_id is not None else None
        if gate is None:
            return ()
        return tuple(entry.path for entry in gate.reads if is_audited_gate_read(entry.path))

    @staticmethod
    def _committed_revision_binding(
        events: list[dict[str, object]],
        interrupt_id: str,
    ) -> RevisionViewBinding | None:
        for event in reversed(events):
            if event.get("type") != "graph_interrupted":
                continue
            if event.get("interrupt_id") != interrupt_id:
                continue
            owner = event.get("revision_owner_invocation_id")
            base = event.get("revision_base_tree_id")
            view = event.get("revision_view")
            paths = event.get("revision_paths")
            before = event.get("revision_before_sha256")
            if (
                not isinstance(owner, str)
                or not isinstance(base, str)
                or not isinstance(view, str)
                or not isinstance(paths, list)
                or not isinstance(before, dict)
            ):
                return None
            logical_paths = tuple(str(path) for path in paths)
            baseline = tuple(
                RevisionPathBaseline(logical_path=str(path), sha256=str(before[path]))
                for path in logical_paths
                if path in before
            )
            if len(baseline) != len(logical_paths):
                return None
            return RevisionViewBinding(
                interrupt_id=interrupt_id,
                owner_invocation_id=owner,
                base_tree_id=base,
                view_relpath=view,
                logical_paths=logical_paths,
                baseline=baseline,
            )
        return None

    @staticmethod
    def _materialize_view(
        change_dir: Path,
        interrupt_id: str,
        sources: dict[str, Path],
    ) -> str:
        rel = f".graph-runtime/views/{interrupt_id}"
        dest_root = change_dir / rel
        if dest_root.exists():
            # Prior materialization may be mode 0555 / files 0444; make writable before replace.
            for path in [dest_root, *dest_root.rglob("*")]:
                try:
                    os.chmod(path, 0o755 if path.is_dir() else 0o644)
                except OSError:
                    pass
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
