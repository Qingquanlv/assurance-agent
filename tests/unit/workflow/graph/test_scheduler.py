"""确定性 wave 选择、真并行执行与 pending-write Update。

覆盖：资源冲突贪心选择与重排稳定性、Barrier 证明的重叠执行区间、
A 成功/B 瞬时失败 resume 只跑 B、A 成功/B 永久失败保留 pending、
Update 失败后不 materialize、声明不冲突但实际重叠写 fail closed。
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from assurance_agent.workflow.core.events import append_event_strict, read_events_strict
from assurance_agent.workflow.core.graph_types import ErrorKind
from assurance_agent.workflow.graph.checkpoint import CheckpointStore, project_invocation
from assurance_agent.workflow.graph.contracts import ResourceClaims, ResourcePath
from assurance_agent.workflow.graph.models import (
    ExecutableTask,
    GraphProjection,
    PlanResult,
    RuntimeContext,
    TaskResult,
    WaveResult,
)
from assurance_agent.workflow.graph.scheduler import Scheduler, select_wave
from assurance_agent.workflow.graph.schema_v2 import (
    BackoffDef,
    RetryPolicyDef,
    TimeoutPolicyDef,
)
from assurance_agent.workflow.graph.workspace import TreeStore, WorkspaceBackend

_INV = "inv-1"
_DIGEST = "g" * 64


def _claims(
    *, writes: tuple[str, ...] = (), reads: tuple[str, ...] = (), exclusive: tuple[str, ...] = ()
) -> ResourceClaims:
    w = tuple(ResourcePath.parse(p) for p in writes)
    r = tuple(ResourcePath.parse(p) for p in reads)
    return ResourceClaims(reads=r, writes=w, exclusive=exclusive, authorization_writes=w)


def _task(
    task_id: str,
    *,
    node_id: str | None = None,
    writes: tuple[str, ...] = (),
    reads: tuple[str, ...] = (),
    exclusive: tuple[str, ...] = (),
    topology_rank: int = 0,
    declaration_index: int = 0,
    target: str = "op:test",
    max_attempts: int = 3,
    retry_on: list[ErrorKind] | None = None,
    retryable: tuple[ErrorKind, ...] = ("timeout", "transport"),
    input_payload: object | None = None,
) -> ExecutableTask:
    return ExecutableTask(
        task_id=task_id,
        invocation_id=_INV,
        checkpoint_ns=_INV,
        graph_id="main",
        node_id=node_id or task_id,
        structural_path="main",
        input=input_payload if input_payload is not None else {"with": {}, "outputs": []},
        input_sha256=f"in-{task_id}",
        contract_digest="cd-1",
        retryable_errors=retryable,
        retry_policy=RetryPolicyDef(
            max_attempts=max_attempts,
            retry_on=retry_on if retry_on is not None else ["timeout", "transport"],
            backoff=BackoffDef(initial_seconds=0.01, multiplier=1.0, max_seconds=0.01, jitter=False),
        ),
        timeout_policy=TimeoutPolicyDef(run_seconds=30.0, heartbeat_seconds=0.05),
        target=target,
        resources=_claims(writes=writes, reads=reads, exclusive=exclusive),
        topology_rank=topology_rank,
        declaration_index=declaration_index,
    )


def _ready_quartet() -> tuple[ExecutableTask, ExecutableTask, ExecutableTask, ExecutableTask]:
    """声明序：API writer、API reader、E2E writer、global-exclusive。"""
    api_w = _task(
        "api-writer",
        writes=("repo:tests/api/**",),
        topology_rank=0,
        declaration_index=0,
    )
    api_r = _task(
        "api-reader",
        reads=("repo:tests/api/**",),
        topology_rank=0,
        declaration_index=1,
    )
    e2e_w = _task(
        "e2e-writer",
        writes=("repo:tests/e2e/**",),
        topology_rank=0,
        declaration_index=2,
    )
    global_x = _task(
        "global-exclusive",
        exclusive=("global:exclusive",),
        topology_rank=0,
        declaration_index=3,
    )
    return api_w, api_r, e2e_w, global_x


def test_select_wave_greedy_maximal_non_conflicting() -> None:
    api_w, api_r, e2e_w, global_x = _ready_quartet()
    wave1 = select_wave([api_w, api_r, e2e_w, global_x], max_parallel_tasks=4)
    assert [t.task_id for t in wave1] == ["api-writer", "e2e-writer"]

    remaining = [api_r, global_x]
    wave2 = select_wave(remaining, max_parallel_tasks=4)
    assert [t.task_id for t in wave2] == ["api-reader"]

    wave3 = select_wave([global_x], max_parallel_tasks=4)
    assert [t.task_id for t in wave3] == ["global-exclusive"]


def test_select_wave_stable_after_input_reorder() -> None:
    api_w, api_r, e2e_w, global_x = _ready_quartet()
    shuffled = [global_x, e2e_w, api_r, api_w]
    wave = select_wave(shuffled, max_parallel_tasks=4)
    assert [t.task_id for t in wave] == ["api-writer", "e2e-writer"]


def test_wave_result_model_shape() -> None:
    result = WaveResult(superstep_id="ss-1", succeeded=("a",), pending_write_set_ids=("ws-1",))
    assert result.failed == ()
    assert result.retry_at is None


# ---------------------------------------------------------------------------
# 真并行 + Execute/Update fixtures
# ---------------------------------------------------------------------------


def _make_project(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    change = project / "qa" / "changes" / "CH-1"
    change.mkdir(parents=True)
    (project / "tests" / "api").mkdir(parents=True)
    (project / "tests" / "e2e").mkdir(parents=True)
    (project / "tests" / "api" / "base.py").write_text("base\n", encoding="utf-8")
    (project / "tests" / "e2e" / "base.py").write_text("base\n", encoding="utf-8")
    (change / "note.txt").write_text("change\n", encoding="utf-8")
    return project


def _seed_invocation(change: Path, tree_id: str) -> None:
    append_event_strict(
        change,
        {
            "source": "graph",
            "type": "graph_invocation_started",
            "invocation_id": _INV,
            "entrypoint": "full",
            "graph_id": "main",
            "graph_digest": _DIGEST,
            "contract_digests": {},
            "params": {},
            "params_sha256": "p" * 64,
            "root_tree_id": tree_id,
            "max_parallel_tasks": 4,
            "checkpoint_ns": _INV,
            "structural_path": "main",
        },
    )


def _context(project: Path) -> RuntimeContext:
    change = project / "qa" / "changes" / "CH-1"
    return RuntimeContext(
        project_root=project,
        repo_root=project,
        change_dir=change,
        change_id="CH-1",
    )


def _projection(change: Path, tree_id: str) -> GraphProjection:
    return project_invocation(change, _INV).model_copy(
        update={"current_tree_id": tree_id, "root_tree_id": tree_id, "graph_digest": _DIGEST}
    )


def _plan(*tasks: ExecutableTask) -> PlanResult:
    return PlanResult(
        superstep_id="ss-1",
        checkpoint_id="bootstrap",
        tasks=tasks,
    )


class _ScriptedRunner:
    """按 task_id 分发的测试用 NodeRunner。"""

    def __init__(self, handlers: dict[str, object]) -> None:
        self._handlers = handlers
        self.calls: list[str] = []

    def execute(self, task: ExecutableTask, workspace, context: RuntimeContext) -> TaskResult:
        self.calls.append(task.task_id)
        handler = self._handlers[task.task_id]
        return handler(task, workspace, context)  # type: ignore[operator]


def test_non_conflicting_tasks_overlap(tmp_path: Path) -> None:
    """两个 handler 在 Barrier(2) 上会合：串行调度无法在超时内完成。"""
    project = _make_project(tmp_path)
    change = project / "qa" / "changes" / "CH-1"
    store = TreeStore(change)
    tree_id = store.capture(project)
    _seed_invocation(change, tree_id)

    barrier = threading.Barrier(2, timeout=2.0)
    intervals: dict[str, tuple[float, float]] = {}
    lock = threading.Lock()

    def make_handler(task_id: str):
        def _run(task: ExecutableTask, workspace, context: RuntimeContext) -> TaskResult:
            start = time.monotonic()
            barrier.wait()
            end = time.monotonic()
            with lock:
                intervals[task_id] = (start, end)
            path = (
                workspace.project_root
                / "tests"
                / ("api" if task_id.endswith("-a") else "e2e")
                / f"{task_id}.py"
            )
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"{task_id}\n", encoding="utf-8")
            return TaskResult(status="succeeded")

        return _run

    task_a = _task(
        "task-a",
        writes=("repo:tests/api/**",),
        declaration_index=0,
        input_payload={"with": {}, "outputs": ["repo:tests/api/task-a.py"]},
    )
    task_b = _task(
        "task-b",
        writes=("repo:tests/e2e/**",),
        declaration_index=1,
        input_payload={"with": {}, "outputs": ["repo:tests/e2e/task-b.py"]},
    )
    runner = _ScriptedRunner({"task-a": make_handler("task-a"), "task-b": make_handler("task-b")})
    scheduler = Scheduler(
        checkpoints=CheckpointStore(change),
        object_store=store,
        workspace_backend=WorkspaceBackend(change),
        node_runner=runner,
        max_parallel_tasks=2,
    )
    # 串行不可能：每个 handler 在 barrier 上最多等 ~2s；若串行第二个永远等不到。
    # 真并行应在远小于 2s 内双方会合并完成。
    t0 = time.monotonic()
    result = scheduler.execute(
        _plan(task_a, task_b),
        _projection(change, tree_id),
        _context(project),
    )
    elapsed = time.monotonic() - t0
    assert elapsed < 1.5, f"expected parallel overlap, took {elapsed:.3f}s"
    assert set(result.succeeded) == {"task-a", "task-b"}
    assert "task-a" in intervals and "task-b" in intervals
    a0, a1 = intervals["task-a"]
    b0, b1 = intervals["task-b"]
    assert a0 < b1 and b0 < a1, "monotonic intervals must overlap"


def test_success_then_transient_fail_resume_only_retries_failed(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    change = project / "qa" / "changes" / "CH-1"
    store = TreeStore(change)
    tree_id = store.capture(project)
    _seed_invocation(change, tree_id)

    calls = {"b": 0}

    def handler_a(task, workspace, context) -> TaskResult:
        path = workspace.project_root / "tests" / "api" / "a.py"
        path.write_text("a\n", encoding="utf-8")
        return TaskResult(status="succeeded")

    def handler_b(task, workspace, context) -> TaskResult:
        calls["b"] += 1
        if calls["b"] == 1:
            return TaskResult(status="failed", error_kind="timeout", error="transient")
        path = workspace.project_root / "tests" / "e2e" / "b.py"
        path.write_text("b\n", encoding="utf-8")
        return TaskResult(status="succeeded")

    task_a = _task(
        "task-a",
        writes=("repo:tests/api/**",),
        declaration_index=0,
        input_payload={"with": {}, "outputs": ["repo:tests/api/a.py"]},
    )
    task_b = _task(
        "task-b",
        writes=("repo:tests/e2e/**",),
        declaration_index=1,
        input_payload={"with": {}, "outputs": ["repo:tests/e2e/b.py"]},
    )
    runner = _ScriptedRunner({"task-a": handler_a, "task-b": handler_b})
    scheduler = Scheduler(
        checkpoints=CheckpointStore(change),
        object_store=store,
        workspace_backend=WorkspaceBackend(change),
        node_runner=runner,
        max_parallel_tasks=2,
    )
    ctx = _context(project)
    first = scheduler.execute(_plan(task_a, task_b), _projection(change, tree_id), ctx)
    assert "task-a" in first.succeeded
    assert "task-b" in first.failed
    assert first.pending_write_set_ids  # A 的 write-set 保持 pending
    events = read_events_strict(change)
    assert not any(e.get("type") == "superstep_committed" for e in events)

    # 尊重已持久化 backoff：略等过 next_retry_at 再 resume。
    failed_events = [e for e in events if e.get("type") == "task_attempt_failed"]
    assert failed_events
    retry_at = failed_events[-1].get("next_retry_at")
    if isinstance(retry_at, str):
        from datetime import datetime, timezone

        target = datetime.fromisoformat(retry_at)
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
        delay = (target - datetime.now(timezone.utc)).total_seconds()
        if delay > 0:
            time.sleep(delay + 0.02)

    live = project_invocation(change, _INV)
    second = scheduler.execute(_plan(task_a, task_b), live, ctx)
    assert set(second.succeeded) == {"task-a", "task-b"}
    assert any(e.get("type") == "superstep_committed" for e in read_events_strict(change))
    # A 只执行一次；B 两次（失败 + 成功）。
    assert runner.calls.count("task-a") == 1
    assert runner.calls.count("task-b") == 2
    assert (project / "tests" / "api" / "a.py").read_text() == "a\n"
    assert (project / "tests" / "e2e" / "b.py").read_text() == "b\n"


def test_success_and_permanent_fail_retains_pending_writes(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    change = project / "qa" / "changes" / "CH-1"
    store = TreeStore(change)
    tree_id = store.capture(project)
    _seed_invocation(change, tree_id)

    def handler_a(task, workspace, context) -> TaskResult:
        (workspace.project_root / "tests" / "api" / "a.py").write_text("a\n", encoding="utf-8")
        return TaskResult(status="succeeded")

    def handler_b(task, workspace, context) -> TaskResult:
        return TaskResult(status="failed", error_kind="contract", error="permanent")

    task_a = _task(
        "task-a",
        writes=("repo:tests/api/**",),
        declaration_index=0,
        input_payload={"with": {}, "outputs": ["repo:tests/api/a.py"]},
    )
    task_b = _task(
        "task-b",
        writes=("repo:tests/e2e/**",),
        declaration_index=1,
        retryable=(),
        retry_on=[],
        input_payload={"with": {}, "outputs": ["repo:tests/e2e/b.py"]},
    )
    scheduler = Scheduler(
        checkpoints=CheckpointStore(change),
        object_store=store,
        workspace_backend=WorkspaceBackend(change),
        node_runner=_ScriptedRunner({"task-a": handler_a, "task-b": handler_b}),
        max_parallel_tasks=2,
    )
    result = scheduler.execute(_plan(task_a, task_b), _projection(change, tree_id), _context(project))
    assert "task-a" in result.succeeded
    assert "task-b" in result.failed
    assert result.pending_write_set_ids
    assert not any(e.get("type") == "superstep_committed" for e in read_events_strict(change))
    assert not (project / "tests" / "api" / "a.py").exists()


def test_update_failure_after_both_successes_retains_write_sets(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    change = project / "qa" / "changes" / "CH-1"
    store = TreeStore(change)
    tree_id = store.capture(project)
    _seed_invocation(change, tree_id)

    def handler_a(task, workspace, context) -> TaskResult:
        (workspace.project_root / "tests" / "api" / "a.py").write_text("a\n", encoding="utf-8")
        return TaskResult(status="succeeded")

    def handler_b(task, workspace, context) -> TaskResult:
        (workspace.project_root / "tests" / "e2e" / "b.py").write_text("b\n", encoding="utf-8")
        return TaskResult(status="succeeded")

    task_a = _task(
        "task-a",
        writes=("repo:tests/api/**",),
        declaration_index=0,
        input_payload={"with": {}, "outputs": ["repo:tests/api/a.py"]},
    )
    task_b = _task(
        "task-b",
        writes=("repo:tests/e2e/**",),
        declaration_index=1,
        input_payload={"with": {}, "outputs": ["repo:tests/e2e/b.py"]},
    )
    scheduler = Scheduler(
        checkpoints=CheckpointStore(change),
        object_store=store,
        workspace_backend=WorkspaceBackend(change),
        node_runner=_ScriptedRunner({"task-a": handler_a, "task-b": handler_b}),
        max_parallel_tasks=2,
    )
    from assurance_agent.workflow.graph.workspace import WorkspaceError

    original_merge = store.merge_write_sets

    def failing_merge(write_sets):
        list(write_sets)
        raise WorkspaceError("injected update failure")

    store.merge_write_sets = failing_merge  # type: ignore[method-assign]
    try:
        result = scheduler.execute(_plan(task_a, task_b), _projection(change, tree_id), _context(project))
    finally:
        store.merge_write_sets = original_merge  # type: ignore[method-assign]

    assert set(result.succeeded) == {"task-a", "task-b"}
    assert len(result.pending_write_set_ids) == 2
    assert not any(e.get("type") == "superstep_committed" for e in read_events_strict(change))
    assert not (project / "tests" / "api" / "a.py").exists()


def test_overlapping_actual_writes_fail_closed_despite_non_conflicting_claims(
    tmp_path: Path,
) -> None:
    project = _make_project(tmp_path)
    change = project / "qa" / "changes" / "CH-1"
    store = TreeStore(change)
    tree_id = store.capture(project)
    _seed_invocation(change, tree_id)

    def write_same(task, workspace, context) -> TaskResult:
        # 声明不冲突（不同 glob），实际都写同一文件。
        path = workspace.project_root / "tests" / "api" / "shared.py"
        path.write_text(f"{task.task_id}\n", encoding="utf-8")
        return TaskResult(status="succeeded")

    # ResourceClaims 声明层不冲突，但 authorization 放宽到共同父路径。
    task_a = _task("task-a", declaration_index=0).model_copy(
        update={
            "resources": ResourceClaims(
                writes=(ResourcePath.parse("repo:tests/api/a/**"),),
                authorization_writes=(ResourcePath.parse("repo:tests/api/**"),),
            ),
            "input": {"with": {}, "outputs": ["repo:tests/api/shared.py"]},
        }
    )
    task_b = _task("task-b", declaration_index=1).model_copy(
        update={
            "resources": ResourceClaims(
                writes=(ResourcePath.parse("repo:tests/api/b/**"),),
                authorization_writes=(ResourcePath.parse("repo:tests/api/**"),),
            ),
            "input": {"with": {}, "outputs": ["repo:tests/api/shared.py"]},
        }
    )
    # 声明层不冲突 → 同 wave。
    assert [t.task_id for t in select_wave([task_a, task_b], max_parallel_tasks=2)] == [
        "task-a",
        "task-b",
    ]
    scheduler = Scheduler(
        checkpoints=CheckpointStore(change),
        object_store=store,
        workspace_backend=WorkspaceBackend(change),
        node_runner=_ScriptedRunner({"task-a": write_same, "task-b": write_same}),
        max_parallel_tasks=2,
    )
    result = scheduler.execute(_plan(task_a, task_b), _projection(change, tree_id), _context(project))
    assert set(result.succeeded) == {"task-a", "task-b"}
    assert len(result.pending_write_set_ids) == 2
    # merge 因实际重叠 fail closed → 无 commit / 无 materialize。
    assert not any(e.get("type") == "superstep_committed" for e in read_events_strict(change))
    assert not (project / "tests" / "api" / "shared.py").exists()
