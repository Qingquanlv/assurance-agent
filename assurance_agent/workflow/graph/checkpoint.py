"""Ledger 投影、checkpoint snapshot 与 ``workflow-state.yaml`` 兼容视图。

严格 ledger（``events.jsonl`` 中的 graph 事件）是唯一权威；checkpoint JSON
快照与 ``workflow-state.yaml`` 都只是可重建的投影/缓存。snapshot 仅在其
``event_seq`` 与 digest 三元组（graph/contract/params）和 ledger 投影完全
一致时才被接受，否则从 ledger 重建并覆盖缓存。``workflow-state.yaml`` 只经
``ProgressionTxn.set_workflow_state_projection`` 落盘，运行时决策从不读它。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

import yaml
from pydantic import ValidationError

from assurance_agent.workflow.core.events import LedgerIntegrityError, read_events_strict
from assurance_agent.workflow.core.graph_events import (
    GRAPH_EVENT_ADAPTER,
    BudgetConsumedEvent,
    CheckpointImportedEvent,
    FanOutExpandedEvent,
    GraphInterruptedEvent,
    GraphInvocationStartedEvent,
    GraphResumedEvent,
    GraphTerminalEvent,
    NodeActivatedEvent,
    NodeSkippedEvent,
    SuperstepCommittedEvent,
    SuperstepPlannedEvent,
    TaskAttemptAbandonedEvent,
    TaskAttemptFailedEvent,
    TaskAttemptStartedEvent,
    TaskAttemptSucceededEvent,
    TaskImportedEvent,
)
from assurance_agent.workflow.core.progression import transaction
from assurance_agent.workflow.graph.models import (
    FanOutExpansion,
    GraphProjection,
    InterruptProjection,
    TaskProjection,
    WorkflowStateProjection,
)

CHECKPOINT_DIR_RELPATH = ".graph-runtime/checkpoints"
_LEDGER_ENVELOPE_KEYS = frozenset({"seq", "ts"})
_TERMINAL_BY_TYPE: dict[str, Literal["completed", "stopped", "failed"]] = {
    "graph_completed": "completed",
    "graph_stopped": "stopped",
    "graph_failed": "failed",
}

_AttemptOutcomeEvent = TaskAttemptSucceededEvent | TaskAttemptFailedEvent | TaskAttemptAbandonedEvent


def _require_task(tasks: dict[str, TaskProjection], event: _AttemptOutcomeEvent) -> TaskProjection:
    prev = tasks.get(event.task_id)
    if prev is None:
        raise LedgerIntegrityError(f"{event.type} references unknown task {event.task_id}")
    return prev


def _imported_task_id(event: TaskImportedEvent, fan_outs: dict[str, FanOutExpansion]) -> str:
    """导入事件的 task ID：fan-out child 复用 expansion 冻结的 ID，否则按 structural path 派生。"""
    if event.task_key is not None:
        expansion = fan_outs.get(event.node_id)
        if expansion is not None and event.task_key in expansion.task_keys:
            return expansion.task_ids[expansion.task_keys.index(event.task_key)]
        return f"{event.structural_path}:{event.node_id}:{event.task_key}"
    return f"{event.structural_path}:{event.node_id}"


def fold_invocation_events(invocation_id: str, events: list[dict[str, object]]) -> GraphProjection:
    """纯函数：把 strict ledger 事件折叠成 ``GraphProjection``（不触碰磁盘）。

    只折叠 ``source == "graph"`` 且属于该 invocation 的事件；投影绝不反向
    覆盖 ledger。重复 ``graph_invocation_started``、未知 task 的 attempt 结局、
    未配对的 ``graph_resumed``、重复 ``budget_consumed``（按
    ``(invocation_id, budget_id, consumption_id)`` 去重）都是完整性失败。
    """
    started: GraphInvocationStartedEvent | None = None
    event_seq = 0
    supersteps = 0
    current_tree_id = ""
    latest_checkpoint_id: str | None = None
    state_values: dict[str, object] = {}
    tasks: dict[str, TaskProjection] = {}
    budgets: dict[str, int] = {}
    seen_consumptions: set[tuple[str, str]] = set()
    fan_outs: dict[str, FanOutExpansion] = {}
    interrupts: dict[str, InterruptProjection] = {}
    terminal: Literal["completed", "stopped", "failed"] | None = None
    terminal_reason: str | None = None

    for raw in events:
        if raw.get("source") != "graph":
            continue
        payload = {k: v for k, v in raw.items() if k not in _LEDGER_ENVELOPE_KEYS}
        try:
            event = GRAPH_EVENT_ADAPTER.validate_python(payload)
        except ValidationError as exc:
            raise LedgerIntegrityError(f"invalid graph event payload: {exc}") from exc
        if event.invocation_id != invocation_id:
            continue
        seq = raw.get("seq")
        if isinstance(seq, int) and not isinstance(seq, bool):
            event_seq = max(event_seq, seq)

        if isinstance(event, GraphInvocationStartedEvent):
            if started is not None:
                raise LedgerIntegrityError(
                    f"duplicate graph_invocation_started for invocation {invocation_id}"
                )
            started = event
            current_tree_id = event.root_tree_id
        elif isinstance(event, (NodeActivatedEvent, NodeSkippedEvent)):
            pass  # node 级摘要由 task 投影派生；activation 不进入持久投影
        elif isinstance(event, FanOutExpandedEvent):
            fan_outs[event.node_id] = FanOutExpansion(
                items=tuple(event.items),
                task_keys=tuple(event.task_keys),
                task_ids=tuple(event.task_ids),
                source_reads_sha256=dict(event.source_reads_sha256),
            )
        elif isinstance(event, SuperstepPlannedEvent):
            supersteps += 1
        elif isinstance(event, TaskAttemptStartedEvent):
            prev = tasks.get(event.task_id)
            tasks[event.task_id] = TaskProjection(
                task_id=event.task_id,
                node_id=event.node_id,
                status="running",
                attempts_used=max(prev.attempts_used if prev else 0, event.attempt_number),
                latest_attempt_id=event.attempt_id,
                write_set_id=prev.write_set_id if prev else None,
                outputs_sha256=prev.outputs_sha256 if prev else {},
                gate_report=prev.gate_report if prev else None,
                state_updates=prev.state_updates if prev else {},
                lease_expires_at=event.lease_expires_at,
            )
        elif isinstance(event, TaskAttemptSucceededEvent):
            prev = _require_task(tasks, event)
            tasks[event.task_id] = prev.model_copy(
                update={
                    "status": "succeeded",
                    "latest_attempt_id": event.attempt_id,
                    "write_set_id": event.write_set_id,
                    "outputs_sha256": dict(event.outputs_sha256),
                    "gate_report": event.gate_report,
                    "state_updates": dict(event.state_updates),
                    "value": event.value,
                    "error_kind": None,
                    "next_retry_at": None,
                }
            )
        elif isinstance(event, TaskAttemptFailedEvent):
            prev = _require_task(tasks, event)
            tasks[event.task_id] = prev.model_copy(
                update={
                    "status": "failed",
                    "latest_attempt_id": event.attempt_id,
                    "error_kind": event.error_kind,
                    "next_retry_at": event.next_retry_at,
                }
            )
        elif isinstance(event, TaskAttemptAbandonedEvent):
            prev = _require_task(tasks, event)
            tasks[event.task_id] = prev.model_copy(
                update={"status": "abandoned", "latest_attempt_id": event.attempt_id}
            )
        elif isinstance(event, BudgetConsumedEvent):
            key = (event.budget_id, event.consumption_id)
            if key in seen_consumptions:
                raise LedgerIntegrityError(
                    "duplicate budget_consumed event for "
                    f"(invocation {invocation_id}, budget {event.budget_id}, "
                    f"consumption {event.consumption_id})"
                )
            seen_consumptions.add(key)
            budgets[event.budget_id] = budgets.get(event.budget_id, 0) + 1
        elif isinstance(event, GraphInterruptedEvent):
            interrupts[event.interrupt_id] = InterruptProjection(
                interrupt_id=event.interrupt_id,
                checkpoint_ns=event.checkpoint_ns,
                node_id=event.node_id,
                checkpoint=event.checkpoint,
                actions=tuple(event.actions),
                audited_reads_sha256=dict(event.audited_reads_sha256),
                artifact_view=event.artifact_view,
            )
            # 嵌套 child 上抛的 interrupt：父 task 不能算成功完成，否则 resume
            # 不会重进 SubgraphHandler。同 namespace 的 builtin:interrupt 节点保持
            # succeeded，以便 resolved 后按 resume.action 路由。
            if started is not None and event.checkpoint_ns != started.checkpoint_ns:
                for task_id, task in list(tasks.items()):
                    if task.node_id == event.node_id and task.status == "succeeded":
                        tasks[task_id] = task.model_copy(update={"status": "interrupted"})
        elif isinstance(event, GraphResumedEvent):
            pending = interrupts.get(event.interrupt_id)
            if pending is None:
                raise LedgerIntegrityError(
                    f"graph_resumed references unknown interrupt {event.interrupt_id} "
                    f"in invocation {invocation_id}"
                )
            interrupts[event.interrupt_id] = pending.model_copy(
                update={"resolved_action": event.action}
            )
        elif isinstance(event, SuperstepCommittedEvent):
            # sibling state 直到 Update（commit）才可见：state_values 只在这里推进。
            latest_checkpoint_id = event.checkpoint_id
            current_tree_id = event.target_tree_id
            state_values = dict(event.state_values)
        elif isinstance(event, GraphTerminalEvent):
            terminal = _TERMINAL_BY_TYPE[event.type]
            terminal_reason = event.reason
        elif isinstance(event, TaskImportedEvent):
            task_id = _imported_task_id(event, fan_outs)
            if task_id in tasks:
                raise LedgerIntegrityError(
                    f"task_imported duplicates existing task {task_id} in invocation {invocation_id}"
                )
            tasks[task_id] = TaskProjection(
                task_id=task_id,
                node_id=event.node_id,
                status="succeeded",
                attempts_used=0,  # 导入不伪造物理 attempt
                outputs_sha256=dict(event.outputs_sha256),
                gate_report=event.gate_report,
            )
        elif isinstance(event, CheckpointImportedEvent):
            pass  # fixture 导入记录不改动任务/预算投影

    if started is None:
        raise LedgerIntegrityError(f"no graph_invocation_started event for invocation {invocation_id}")
    return GraphProjection(
        invocation_id=started.invocation_id,
        entrypoint=started.entrypoint,
        checkpoint_ns=started.checkpoint_ns,
        parent_invocation_id=started.parent_invocation_id,
        parent_task_id=started.parent_task_id,
        structural_path=started.structural_path,
        graph_digest=started.graph_digest,
        contract_digests=dict(started.contract_digests),
        params=dict(started.params),
        root_tree_id=started.root_tree_id,
        current_tree_id=current_tree_id,
        latest_checkpoint_id=latest_checkpoint_id,
        event_seq=event_seq,
        supersteps=supersteps,
        state_values=state_values,
        tasks=tasks,
        budgets=budgets,
        fan_out_expansions=fan_outs,
        interrupts=interrupts,
        terminal=terminal,
        terminal_reason=terminal_reason,
    )


def project_invocation(change_dir: Path, invocation_id: str) -> GraphProjection:
    """从 strict ledger 重建指定 invocation 的投影（ledger 是唯一权威）。"""
    return fold_invocation_events(invocation_id, read_events_strict(change_dir))


def project_workflow_state(projection: GraphProjection) -> WorkflowStateProjection:
    """派生 ``workflow-state.yaml`` 兼容视图；每个字段仅来自 ``GraphProjection``。"""
    nodes: dict[str, list[str]] = {}
    for task_id in sorted(projection.tasks):
        nodes.setdefault(projection.tasks[task_id].node_id, []).append(task_id)
    return WorkflowStateProjection(
        invocation_id=projection.invocation_id,
        entrypoint=projection.entrypoint,
        terminal=projection.terminal,
        terminal_reason=projection.terminal_reason,
        latest_checkpoint_id=projection.latest_checkpoint_id,
        event_seq=projection.event_seq,
        nodes={node_id: tuple(task_ids) for node_id, task_ids in sorted(nodes.items())},
        tasks={task_id: projection.tasks[task_id] for task_id in sorted(projection.tasks)},
        pending_interrupts=tuple(
            interrupt
            for _, interrupt in sorted(projection.interrupts.items())
            if interrupt.resolved_action is None
        ),
        budgets=dict(sorted(projection.budgets.items())),
    )


def render_workflow_state_yaml(projection: GraphProjection) -> bytes:
    """把兼容视图渲染成 ``workflow-state.yaml`` 字节（供 projection staging 落盘）。"""
    view = project_workflow_state(projection)
    text = yaml.safe_dump(view.model_dump(mode="json"), sort_keys=False, allow_unicode=True)
    return text.encode("utf-8")


def checkpoint_snapshot_relpath(projection: GraphProjection) -> str:
    name = projection.latest_checkpoint_id or f"bootstrap-{projection.invocation_id}"
    return f"{CHECKPOINT_DIR_RELPATH}/{name}.json"


def dump_checkpoint_snapshot(projection: GraphProjection) -> bytes:
    payload = json.dumps(
        projection.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    )
    return (payload + "\n").encode("utf-8")


class CheckpointStore:
    """checkpoint snapshot 缓存：snapshot 仅是性能缓存，ledger 才是权威。"""

    def __init__(self, change_dir: Path) -> None:
        self._change_dir = change_dir

    def write(self, projection: GraphProjection) -> Path:
        rel = checkpoint_snapshot_relpath(projection)
        with transaction(self._change_dir) as txn:
            txn.write_file(rel, dump_checkpoint_snapshot(projection))
        return self._change_dir / rel

    def read_latest(self, invocation_id: str) -> GraphProjection:
        projection = project_invocation(self._change_dir, invocation_id)
        if not self._snapshot_matches(projection):
            self.write(projection)
        return projection

    def project(self, invocation_id: str) -> GraphProjection:
        """ledger 权威投影，并修复落后/损坏的 checkpoint 与 workflow-state 缓存。"""
        projection = project_invocation(self._change_dir, invocation_id)
        if not self._snapshot_matches(projection):
            self.write(projection)
        self._repair_workflow_state(projection)
        return projection

    def latest_root_invocation(self) -> str | None:
        """严格 ledger 中最近一次无 parent 的 root ``graph_invocation_started``。"""
        latest: str | None = None
        for raw in read_events_strict(self._change_dir):
            if raw.get("source") != "graph" or raw.get("type") != "graph_invocation_started":
                continue
            if raw.get("parent_invocation_id") is not None:
                continue
            invocation_id = raw.get("invocation_id")
            if isinstance(invocation_id, str):
                latest = invocation_id
        return latest

    def _repair_workflow_state(self, projection: GraphProjection) -> None:
        """缺失或损坏的 ``workflow-state.yaml`` 只能从 ledger 投影重建，绝不反向推断。"""
        path = self._change_dir / "workflow-state.yaml"
        expected = render_workflow_state_yaml(projection)
        try:
            current = path.read_bytes()
        except OSError:
            current = b""
        if current == expected:
            return
        with transaction(self._change_dir) as txn:
            txn.set_workflow_state_projection(expected)

    def _snapshot_matches(self, projection: GraphProjection) -> bool:
        """仅当 snapshot 的 event_seq 与 digest 三元组和 ledger 投影一致时接受。"""
        path = self._change_dir / checkpoint_snapshot_relpath(projection)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        try:
            cached = GraphProjection.model_validate(raw)
        except ValidationError:
            return False
        return (
            cached.invocation_id == projection.invocation_id
            and cached.event_seq == projection.event_seq
            and cached.graph_digest == projection.graph_digest
            and cached.contract_digests == projection.contract_digests
            and cached.params == projection.params
        )


__all__ = [
    "CHECKPOINT_DIR_RELPATH",
    "CheckpointStore",
    "checkpoint_snapshot_relpath",
    "dump_checkpoint_snapshot",
    "fold_invocation_events",
    "project_invocation",
    "project_workflow_state",
    "render_workflow_state_yaml",
]
