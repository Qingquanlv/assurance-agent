"""纯只读状态查询：一次 read_events_strict，全部派生共用同一 events 列表，零文件写入。

与 CheckpointStore.project() 的区别：本模块不修 snapshot、不写 workflow-state.yaml，
供 CLI 只读查询使用；runtime 写路径继续用 CheckpointStore（修缓存是其本职）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from assurance_agent.workflow.core.events import read_events_strict
from assurance_agent.workflow.graph.checkpoint import (
    fold_invocation_events,
    latest_root_invocation_id,
)
from assurance_agent.workflow.graph.models import GraphProjection, GraphStatus


def pending_write_sets(events: list[dict[str, object]], invocation_id: str) -> tuple[str, ...]:
    """succeeded 但尚未随 superstep commit 的 write-set（保序，按 ledger 出现顺序）。"""
    succeeded: list[str] = []
    committed: set[str] = set()
    for raw in events:
        if raw.get("source") != "graph" or raw.get("invocation_id") != invocation_id:
            continue
        if raw.get("type") == "task_attempt_succeeded":
            write_set_id = raw.get("write_set_id")
            if isinstance(write_set_id, str):
                succeeded.append(write_set_id)
        elif raw.get("type") == "superstep_committed":
            raw_ids = raw.get("write_set_ids")
            for write_set_id in raw_ids if isinstance(raw_ids, list) else []:
                if isinstance(write_set_id, str):
                    committed.add(write_set_id)
    return tuple(ws for ws in succeeded if ws not in committed)


def graph_status_from_projection(
    projection: GraphProjection,
    *,
    pending_write_sets: tuple[str, ...] = (),
    recovery_state: Literal["revision_resume_recovery_pending"] | None = None,
) -> GraphStatus:
    """从 ledger 投影派生公开 GraphStatus（不读 workflow-state.yaml）。"""
    pending_interrupts = tuple(
        interrupt
        for _, interrupt in sorted(projection.interrupts.items())
        if interrupt.resolved_action is None
    )
    running = tuple(sorted(task_id for task_id, task in projection.tasks.items() if task.status == "running"))
    pending = tuple(
        sorted(
            task_id for task_id, task in projection.tasks.items() if task.status in ("failed", "abandoned")
        )
    )
    retry_ats = [task.next_retry_at for task in projection.tasks.values() if task.next_retry_at is not None]
    if projection.terminal == "completed":
        status: Literal["running", "interrupted", "completed", "stopped", "failed"] = "completed"
    elif projection.terminal == "stopped":
        status = "stopped"
    elif projection.terminal == "failed":
        status = "failed"
    elif pending_interrupts:
        status = "interrupted"
    else:
        status = "running"
    return GraphStatus(
        invocation_id=projection.invocation_id,
        entrypoint=projection.entrypoint,
        status=status,
        checkpoint_id=projection.latest_checkpoint_id,
        event_seq=projection.event_seq,
        superstep=projection.supersteps,
        running_tasks=running,
        pending_tasks=pending,
        pending_write_sets=pending_write_sets,
        pending_interrupts=pending_interrupts,
        next_retry_at=min(retry_ats) if retry_ats else None,
        budgets=dict(sorted(projection.budgets.items())),
        terminal_reason=projection.terminal_reason,
        recovery_state=recovery_state,
    )


def read_latest_graph_status(change_dir: Path, entrypoint: str | None = None) -> GraphStatus | None:
    """单一 ledger 快照：读一次 events，latest/projection/pending_write_sets 共用。"""
    from assurance_agent.workflow.graph.manual_revision import derive_revision_recovery_state

    events = read_events_strict(change_dir)
    invocation_id = latest_root_invocation_id(events, entrypoint)
    if invocation_id is None:
        return None
    projection = fold_invocation_events(invocation_id, events)
    return graph_status_from_projection(
        projection,
        pending_write_sets=pending_write_sets(events, invocation_id),
        recovery_state=derive_revision_recovery_state(events),
    )
