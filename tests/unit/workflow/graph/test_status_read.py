"""只读状态查询路径：纯函数、单一快照、零写入、集合构成冻结。"""

from pathlib import Path

from assurance_agent.workflow.core.events import read_events_strict
from assurance_agent.workflow.core.graph_events import (
    GraphInvocationStartedEvent,
    GraphTerminalEvent,
    NodeActivatedEvent,
    SuperstepCommittedEvent,
    SuperstepPlannedEvent,
    TaskAttemptStartedEvent,
    TaskAttemptSucceededEvent,
)
from assurance_agent.workflow.core.progression import transaction
from assurance_agent.workflow.graph.checkpoint import (
    CheckpointStore,
    latest_root_invocation_id,
)
from assurance_agent.workflow.graph.models import GraphProjection, TaskProjection
from assurance_agent.workflow.graph.status import (
    graph_status_from_projection,
    pending_write_sets,
    read_latest_graph_status,
)

INV = "inv-1"


def _started(invocation_id: str = INV, entrypoint: str = "full") -> GraphInvocationStartedEvent:
    return GraphInvocationStartedEvent(
        type="graph_invocation_started",
        invocation_id=invocation_id,
        entrypoint=entrypoint,
        graph_id="workflow",
        graph_digest="dg",
        contract_digests={},
        params={},
        params_sha256="",
        root_tree_id="tree",
        max_parallel_tasks=1,
        checkpoint_ns=invocation_id,
        structural_path=invocation_id,
    )


def _attempt_started(task_id: str, attempt_id: str) -> TaskAttemptStartedEvent:
    return TaskAttemptStartedEvent(
        type="task_attempt_started",
        invocation_id=INV,
        checkpoint_ns=INV,
        superstep_id="ss-1",
        task_id=task_id,
        attempt_id=attempt_id,
        node_id="n",
        input_sha256="in",
        graph_digest="dg",
        contract_digest="cd",
        attempt_number=1,
        lease_expires_at="2030-01-01T00:00:00Z",
        started_at="2026-01-01T00:00:00Z",
    )


def _attempt_succeeded(task_id: str, attempt_id: str, write_set_id: str) -> TaskAttemptSucceededEvent:
    return TaskAttemptSucceededEvent(
        type="task_attempt_succeeded",
        invocation_id=INV,
        checkpoint_ns=INV,
        superstep_id="ss-1",
        task_id=task_id,
        attempt_id=attempt_id,
        write_set_id=write_set_id,
    )


def seed_ledger(change_dir: Path, events: list) -> None:
    change_dir.mkdir(parents=True, exist_ok=True)
    with transaction(change_dir) as txn:
        for event in events:
            txn.append_strict(event)


def seed_completed(change_dir: Path, invocation_id: str = INV) -> None:
    seed_ledger(
        change_dir,
        [
            _started(invocation_id),
            GraphTerminalEvent(
                type="graph_completed",
                invocation_id=invocation_id,
                checkpoint_ns=invocation_id,
                reason="done",
            ),
        ],
    )


def seed_write_sets(change_dir: Path) -> None:
    """一个已提交 write-set（ws-committed）与一个悬挂 write-set（ws-dangling）。"""
    seed_ledger(
        change_dir,
        [
            _started(),
            # node_activated required so fold can bind generation for committed_task_ids
            # (post-main generation fold; brief fixture omitted this).
            NodeActivatedEvent(
                type="node_activated",
                invocation_id=INV,
                checkpoint_ns=INV,
                graph_id="workflow",
                node_id="n",
                generation_ordinal=0,
                activation_id="n-g0",
                input_sha256="in",
                source_reads_sha256={},
            ),
            SuperstepPlannedEvent(
                type="superstep_planned",
                invocation_id=INV,
                checkpoint_ns=INV,
                superstep_id="ss-1",
                checkpoint_id="cp-1",
                task_ids=["t-1", "t-2"],
            ),
            _attempt_started("t-1", "a-1"),
            _attempt_started("t-2", "a-2"),
            _attempt_succeeded("t-1", "a-1", "ws-committed"),
            _attempt_succeeded("t-2", "a-2", "ws-dangling"),
            SuperstepCommittedEvent(
                type="superstep_committed",
                invocation_id=INV,
                checkpoint_ns=INV,
                superstep_id="ss-1",
                checkpoint_id="cp-1",
                write_set_ids=["ws-committed"],
                target_tree_id="tree",
                state_values={},
                committed_task_ids=["t-1"],
            ),
            GraphTerminalEvent(
                type="graph_completed",
                invocation_id=INV,
                checkpoint_ns=INV,
                reason="done",
            ),
        ],
    )


def tree_snapshot(root: Path) -> dict[str, tuple[int, int]]:
    """relpath -> (size, mtime_ns)，只读性断言用。"""
    return {
        str(p.relative_to(root)): (p.stat().st_size, p.stat().st_mtime_ns)
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def test_read_latest_none_without_invocation(tmp_path: Path) -> None:
    change = tmp_path / "CH-1"
    change.mkdir(parents=True)
    assert read_latest_graph_status(change) is None


def test_read_latest_completed_projection(tmp_path: Path) -> None:
    change = tmp_path / "CH-1"
    seed_completed(change)
    status = read_latest_graph_status(change)
    assert status is not None
    assert status.invocation_id == INV
    assert status.status == "completed"
    assert status.terminal_reason == "done"


def test_read_only_guarantee_against_drifted_caches(tmp_path: Path) -> None:
    """缓存漂移时查询也绝不写文件（对照：CheckpointStore.project 会写）。"""
    change = tmp_path / "CH-1"
    seed_completed(change)
    (change / "workflow-state.yaml").write_text("broken: [", encoding="utf-8")
    snap_dir = change / ".graph-runtime" / "checkpoints"
    snap_dir.mkdir(parents=True, exist_ok=True)
    (snap_dir / "stale.json").write_text("{", encoding="utf-8")
    before = tree_snapshot(change)

    status = read_latest_graph_status(change)
    assert status is not None and status.status == "completed"
    assert tree_snapshot(change) == before  # 文件清单与 mtime 完全不变

    # 对照：同 fixture 经旧写路径会修复缓存（证明上面的断言能检出写入）。
    CheckpointStore(change).project(INV)
    assert tree_snapshot(change) != before


def test_latest_root_invocation_id_entrypoint_filter_and_method_parity(tmp_path: Path) -> None:
    change = tmp_path / "CH-1"
    seed_ledger(change, [_started("inv-a", "full"), _started("inv-b", "retro")])
    events = read_events_strict(change)
    assert latest_root_invocation_id(events) == "inv-b"
    assert latest_root_invocation_id(events, entrypoint="full") == "inv-a"
    # CheckpointStore 方法委托同一函数（各自读一次 ledger，结果一致）。
    assert CheckpointStore(change).latest_root_invocation() == "inv-b"


def test_pending_write_sets_committed_excluded_and_dangling_included(tmp_path: Path) -> None:
    change = tmp_path / "CH-1"
    seed_write_sets(change)
    events = read_events_strict(change)
    assert pending_write_sets(events, INV) == ("ws-dangling",)


def test_pending_tasks_membership_frozen() -> None:
    """集合构成逐例冻结：failed/abandoned 在内；pending/running/succeeded 不在。"""

    def task(tid: str, status: str, **kw: object) -> TaskProjection:
        return TaskProjection(task_id=tid, node_id="n", status=status, **kw)  # type: ignore[arg-type]

    proj = GraphProjection(
        invocation_id=INV,
        entrypoint="full",
        checkpoint_ns=INV,
        structural_path=INV,
        graph_digest="dg",
        contract_digests={},
        params={},
        root_tree_id="t",
        current_tree_id="t",
        tasks={
            "t-failed": task("t-failed", "failed"),
            "t-failed-retry": task("t-failed-retry", "failed", next_retry_at="2030-01-01T00:00:00Z"),
            "t-abandoned": task("t-abandoned", "abandoned"),
            "t-pending": task("t-pending", "pending"),
            "t-running": task("t-running", "running"),
            "t-succeeded": task("t-succeeded", "succeeded"),
        },
    )
    status = graph_status_from_projection(proj)
    assert set(status.pending_tasks) == {"t-failed", "t-failed-retry", "t-abandoned"}


def test_runtime_reexport_compat() -> None:
    from assurance_agent.workflow.graph import runtime

    assert runtime.graph_status_from_projection is graph_status_from_projection


def test_single_ledger_snapshot(monkeypatch, tmp_path: Path) -> None:
    """单一快照红线：read_latest_graph_status 恰好调一次 read_events_strict。"""
    from assurance_agent.workflow.graph import status as status_mod

    change = tmp_path / "CH-1"
    seed_completed(change)
    calls = 0
    real = status_mod.read_events_strict

    def counting(change_dir: Path) -> list[dict[str, object]]:
        nonlocal calls
        calls += 1
        return real(change_dir)

    monkeypatch.setattr(status_mod, "read_events_strict", counting)
    assert status_mod.read_latest_graph_status(change) is not None
    assert calls == 1


def test_equivalence_with_runtime_status(tmp_path: Path) -> None:
    """新旧等价：同一 fixture 下与 runtime.status() 的完整 model_dump 逐键相等。

    runtime.status() 只触 _checkpoints 与 _pending_write_sets，其余依赖不会解引用，
    故可用 None 占位构造（仅为等价对比，非正常使用方式）。
    """
    from typing import Any, cast

    from assurance_agent.workflow.graph.checkpoint import CheckpointStore
    from assurance_agent.workflow.graph.runtime import GraphRuntime

    change = tmp_path / "CH-1"
    seed_write_sets(change)
    runtime = GraphRuntime(
        checkpoint_store=CheckpointStore(change),
        object_store=cast(Any, None),
        workspace_backend=cast(Any, None),
        contracts=cast(Any, None),
        node_runner=cast(Any, None),
        scheduler=cast(Any, None),
        schema_resolver=cast(Any, None),
        clock=cast(Any, None),
    )
    expected = runtime.status(INV).model_dump(mode="json")
    actual = read_latest_graph_status(change)
    assert actual is not None
    assert actual.model_dump(mode="json") == expected
