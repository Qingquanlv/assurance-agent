"""确定性 wave 选择、真并行执行与 pending-write Update。

覆盖：资源冲突贪心选择与重排稳定性、Barrier 证明的重叠执行区间、
A 成功/B 瞬时失败 resume 只跑 B、A 成功/B 永久失败保留 pending、
Update 失败后不 materialize、声明不冲突但实际重叠写 fail closed。
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest
from pydantic import ValidationError

from assurance_agent.workflow.core.events import append_event_strict, read_events_strict
from tests.helpers_graph_v6 import v6_started_bindings
from assurance_agent.workflow.core.graph_types import ErrorKind
from assurance_agent.workflow.graph.checkpoint import CheckpointStore, project_invocation
from assurance_agent.workflow.graph.contracts import (
    ExecutionContract,
    ExecutionContractCatalog,
    ResourceClaims,
    ResourcePath,
    parse_execution_contracts,
)
from assurance_agent.workflow.graph.models import (
    ExecutableTask,
    GraphProjection,
    PlanResult,
    RuntimeContext,
    TaskResult,
    WaveResult,
)
from assurance_agent.workflow.graph.handlers.operation import OperationHandler
from assurance_agent.workflow.graph.project_locks import ProjectResourceConflict
from assurance_agent.workflow.graph.scheduler import Scheduler, select_wave
from assurance_agent.workflow.graph.selected_wave import PreparedWaveLease
from assurance_agent.workflow.graph.schema_v2 import (
    BackoffDef,
    RetryPolicyDef,
    TimeoutPolicyDef,
)
from assurance_agent.workflow.graph.workspace import TreeStore, WorkspaceBackend
from assurance_agent.workflow.graph.task_runner import HandlerNodeRunner

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


def _seed_invocation(change: Path, tree_id: str, *, compiled=None) -> None:
    payload = {
        "source": "graph",
        "type": "graph_invocation_started",
        "invocation_id": _INV,
        "entrypoint": "full",
        "graph_id": "main",
        "graph_digest": compiled.digest if compiled is not None else _DIGEST,
        "contract_digests": dict(compiled.contract_digests) if compiled is not None else {},
        **v6_started_bindings(),
        "params": {},
        "params_sha256": "p" * 64,
        "root_tree_id": tree_id,
        "max_parallel_tasks": 4,
        "checkpoint_ns": _INV,
        "structural_path": "main",
    }
    append_event_strict(change, payload)


def _context(project: Path) -> RuntimeContext:
    change = project / "qa" / "changes" / "CH-1"
    return RuntimeContext(
        project_root=project,
        repo_root=project,
        change_dir=change,
        change_id="CH-1",
    )


def test_runtime_context_rejects_forged_project_lock_tokens(tmp_path: Path) -> None:
    project = _make_project(tmp_path)

    with pytest.raises(ValidationError, match="held_project_lock_tokens"):
        RuntimeContext(
            project_root=project,
            repo_root=project,
            change_dir=project / "qa/changes/CH-1",
            change_id="CH-1",
            held_project_lock_tokens=("project:issue-registry",),  # type: ignore[call-arg]
        )


def test_scheduler_binds_current_attempt_id_into_runtime_context(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    change = project / "qa" / "changes" / "CH-1"
    store = TreeStore(change)
    tree_id = store.capture(project)
    _seed_invocation(change, tree_id)
    seen: list[str | None] = []

    def handler(_task, _workspace, context: RuntimeContext) -> TaskResult:
        seen.append(context.task_attempt_id)
        return TaskResult(status="succeeded")

    task = _task("task-a")
    scheduler = Scheduler(
        checkpoints=CheckpointStore(change),
        object_store=store,
        workspace_backend=WorkspaceBackend(change),
        node_runner=_ScriptedRunner({"task-a": handler}),
    )

    result = scheduler.execute(
        _plan(task),
        _projection(change, tree_id),
        _context(project),
    )

    assert result.succeeded == ("task-a",)
    assert seen == ["task-a-a1"]


def test_expired_inherited_project_lock_context_reacquires(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    change = project / "qa/changes/CH-1"
    locks = _TrackingProjectLocks()
    scheduler = Scheduler(
        checkpoints=CheckpointStore(change),
        object_store=TreeStore(change),
        project_lock_manager=locks,
    )
    context = _context(project)
    tokens = ("project:issue-registry",)

    with scheduler._project_lock_scope(context, tokens) as inherited:  # noqa: SLF001
        assert locks.held is True
    assert locks.held is False

    with scheduler._project_lock_scope(inherited, tokens):  # noqa: SLF001
        assert locks.held is True

    assert locks.calls == [(tokens, 5.0), (tokens, 5.0)]


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


def test_operation_workspace_skips_agent_only_git_index(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    change = project / "qa" / "changes" / "CH-1"
    store = TreeStore(change)
    tree_id = store.capture(project)
    _seed_invocation(change, tree_id)

    target = "operation:test"

    def operation_handler(task, workspace, context) -> TaskResult:
        assert not (workspace.root / ".git").exists()
        output = workspace.project_root / "tests" / "api" / "operation.py"
        output.write_text("operation\n", encoding="utf-8")
        return TaskResult(status="succeeded")

    task = _task(
        "operation-task",
        target=target,
        writes=("repo:tests/api/**",),
        input_payload={"with": {}, "outputs": ["repo:tests/api/operation.py"]},
    )
    scheduler = Scheduler(
        checkpoints=CheckpointStore(change),
        object_store=store,
        workspace_backend=WorkspaceBackend(change),
        node_runner=_ScriptedRunner({task.task_id: operation_handler}),
        contracts=ExecutionContractCatalog(
            contracts={
                target: ExecutionContract(
                    target=target,
                    handler="operation",
                    writes=("repo:tests/api/**",),
                    authorization_writes=("repo:tests/api/**",),
                )
            }
        ),
    )

    result = scheduler.execute(_plan(task), _projection(change, tree_id), _context(project))

    assert result.succeeded == (task.task_id,)


def test_failed_memory_delivery_task_does_not_publish_partial_workspace_writes(
    tmp_path: Path,
) -> None:
    project = _make_project(tmp_path)
    change = project / "qa/changes/CH-1"
    memory = project / ".aa/memory/aa-run.md"
    projection = project / "qa/improvements/improvements.json"
    memory.parent.mkdir(parents=True)
    projection.parent.mkdir(parents=True)
    memory.write_text("# canonical memory\n", encoding="utf-8")
    projection.write_text('{"state":"evaluating"}\n', encoding="utf-8")
    canonical_memory = memory.read_bytes()
    canonical_projection = projection.read_bytes()

    store = TreeStore(change)
    tree_id = store.capture(project)
    _seed_invocation(change, tree_id)
    target = "operation:crashing-memory-delivery"
    synchronized = (
        ResourcePath.parse("project:qa/improvements/**"),
        ResourcePath.parse("project:.aa/memory/**"),
    )
    claims = ResourceClaims(
        writes=synchronized,
        synchronized=synchronized,
        exclusive=("project:improvement-registry",),
        authorization_writes=synchronized,
    )
    task = _task(
        "crashing-memory-delivery",
        target=target,
        max_attempts=1,
        retryable=(),
        retry_on=[],
    ).model_copy(update={"resources": claims})

    def crash_after_private_writes(task, workspace, context) -> TaskResult:  # noqa: ANN001
        del task, context
        (workspace.project_root / ".aa/memory/aa-run.md").write_text(
            "# partial memory\n",
            encoding="utf-8",
        )
        (workspace.project_root / "qa/improvements/improvements.json").write_text(
            '{"state":"applied"}\n',
            encoding="utf-8",
        )
        raise RuntimeError("crash before task success")

    operation_handler = OperationHandler({target: crash_after_private_writes})
    runner = HandlerNodeRunner({target: operation_handler})
    scheduler = Scheduler(
        checkpoints=CheckpointStore(change),
        object_store=store,
        workspace_backend=WorkspaceBackend(change),
        node_runner=runner,
        contracts=ExecutionContractCatalog(
            contracts={
                target: ExecutionContract(
                    target=target,
                    handler="operation",
                    writes=("project:qa/improvements/**", "project:.aa/memory/**"),
                    synchronized=(
                        "project:qa/improvements/**",
                        "project:.aa/memory/**",
                    ),
                    exclusive=("project:improvement-registry",),
                    authorization_writes=(
                        "project:qa/improvements/**",
                        "project:.aa/memory/**",
                    ),
                )
            }
        ),
    )

    result = scheduler.execute(
        _plan(task),
        _projection(change, tree_id),
        _context(project),
    )

    assert result.failed == (task.task_id,)
    assert memory.read_bytes() == canonical_memory
    assert projection.read_bytes() == canonical_projection
    assert not (change / ".graph-runtime/tasks" / task.task_id).exists()


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


# ---------------------------------------------------------------------------
# synchronized project resource lifetime / timeout routing


class _TrackingProjectLocks:
    def __init__(self, *, timeout: bool = False) -> None:
        self.held = False
        self.timeout = timeout
        self.calls: list[tuple[tuple[str, ...], float]] = []

    @contextmanager
    def acquire(self, tokens, timeout_seconds: float):
        self.calls.append((tuple(tokens), timeout_seconds))
        if self.timeout:
            raise ProjectResourceConflict("busy synchronized resource")
        self.held = True
        try:
            yield
        finally:
            self.held = False


class _TokenBlockingProjectLocks:
    def __init__(self, blocked_token: str) -> None:
        self.blocked_token = blocked_token
        self.calls: list[tuple[str, ...]] = []

    @contextmanager
    def acquire(self, tokens, timeout_seconds: float):
        del timeout_seconds
        self.calls.append(tuple(tokens))
        raise ProjectResourceConflict(
            f"busy synchronized resource {self.blocked_token}",
            token=self.blocked_token,
        )
        yield


def _synchronized_task(*, retry_conflicts: bool = False) -> ExecutableTask:
    synchronized = (ResourcePath.parse("project:qa/issues/**"),)
    return _task(
        "update-issue",
        input_payload={
            "with": {},
            "outputs": [
                "project:qa/issues/ISSUE-1.json",
                "change:results/update.json",
            ],
        },
        retry_on=["conflict"] if retry_conflicts else [],
        retryable=("conflict",) if retry_conflicts else (),
    ).model_copy(
        update={
            "resources": ResourceClaims(
                reads=synchronized,
                writes=(*synchronized, ResourcePath.parse("change:results/**")),
                synchronized=synchronized,
                exclusive=("project:issue-registry", "project:zzz"),
                authorization_writes=(
                    *synchronized,
                    ResourcePath.parse("change:results/**"),
                ),
            )
        }
    )


def test_scheduler_holds_project_locks_across_overlay_handler_and_update(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    change = project / "qa/changes/CH-1"
    issue = project / "qa/issues/ISSUE-1.json"
    issue.parent.mkdir(parents=True)
    issue.write_text('{"version":1}\n', encoding="utf-8")
    (project / "app").mkdir()
    (project / "app/source.py").write_text("app base\n", encoding="utf-8")
    store = TreeStore(change)
    invocation_tree = store.capture(project)
    _seed_invocation(change, invocation_tree)
    issue.write_text('{"version":2}\n', encoding="utf-8")
    (project / "app/source.py").write_text("unrelated live version 2\n", encoding="utf-8")
    locks = _TrackingProjectLocks()
    phases: list[str] = []

    overlay = store.overlay_synchronized_paths

    def observed_overlay(base_tree_id, project_root, paths):
        assert locks.held
        phases.append("overlay")
        return overlay(base_tree_id, project_root, paths)

    apply_targeted = store.apply_write_sets_to_synchronized_paths

    def observed_apply(project_root, write_sets, paths):
        assert locks.held
        phases.append("apply")
        return apply_targeted(project_root, write_sets, paths)

    store.overlay_synchronized_paths = observed_overlay  # type: ignore[method-assign]
    store.apply_write_sets_to_synchronized_paths = observed_apply  # type: ignore[method-assign]

    def update_issue(task, workspace, context) -> TaskResult:
        assert locks.held
        phases.append("handler")
        assert (workspace.project_root / "qa/issues/ISSUE-1.json").read_text() == '{"version":2}\n'
        assert (workspace.project_root / "app/source.py").read_text() == "app base\n"
        (workspace.project_root / "qa/issues/ISSUE-1.json").write_text('{"version":3}\n', encoding="utf-8")
        result = workspace.change_dir / "results/update.json"
        result.parent.mkdir(parents=True)
        result.write_text('{"updated":true}\n', encoding="utf-8")
        return TaskResult(status="succeeded")

    scheduler = Scheduler(
        checkpoints=CheckpointStore(change),
        object_store=store,
        workspace_backend=WorkspaceBackend(change),
        node_runner=_ScriptedRunner({"update-issue": update_issue}),
        project_lock_manager=locks,
        project_lock_timeout_seconds=0.25,
    )
    result = scheduler.execute(
        _plan(_synchronized_task()),
        _projection(change, invocation_tree),
        _context(project),
    )

    assert result.succeeded == ("update-issue",)
    assert result.pending_write_set_ids
    assert locks.calls == [(("project:issue-registry", "project:zzz"), 0.25)]
    assert phases == ["overlay", "handler", "apply"]
    assert locks.held is False
    assert issue.read_text() == '{"version":3}\n'
    assert (change / "results/update.json").read_text() == '{"updated":true}\n'
    assert (project / "app/source.py").read_text() == "unrelated live version 2\n"


def test_scheduler_does_not_acquire_project_locks_for_ordinary_claims(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    change = project / "qa/changes/CH-1"
    store = TreeStore(change)
    tree_id = store.capture(project)
    _seed_invocation(change, tree_id)
    locks = _TrackingProjectLocks(timeout=True)
    task = _task("ordinary", reads=("change:input.txt",))
    scheduler = Scheduler(
        checkpoints=CheckpointStore(change),
        object_store=store,
        workspace_backend=WorkspaceBackend(change),
        node_runner=_ScriptedRunner({"ordinary": lambda *_: TaskResult(status="succeeded")}),
        project_lock_manager=locks,
    )

    result = scheduler.execute(_plan(task), _projection(change, tree_id), _context(project))

    assert result.succeeded == ("ordinary",)
    assert locks.calls == []


def test_scheduler_persists_project_lock_timeout_as_retryable_conflict(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    change = project / "qa/changes/CH-1"
    issue = project / "qa/issues/ISSUE-1.json"
    issue.parent.mkdir(parents=True)
    issue.write_text('{"version":1}\n', encoding="utf-8")
    store = TreeStore(change)
    tree_id = store.capture(project)
    _seed_invocation(change, tree_id)
    locks = _TrackingProjectLocks(timeout=True)
    runner = _ScriptedRunner(
        {"update-issue": lambda *_: (_ for _ in ()).throw(AssertionError("runner called"))}
    )
    scheduler = Scheduler(
        checkpoints=CheckpointStore(change),
        object_store=store,
        workspace_backend=WorkspaceBackend(change),
        node_runner=runner,
        project_lock_manager=locks,
        project_lock_timeout_seconds=0.125,
    )

    result = scheduler.execute(
        _plan(_synchronized_task(retry_conflicts=True)),
        _projection(change, tree_id),
        _context(project),
    )

    assert result.failed == ()
    assert result.retry_at is not None
    assert runner.calls == []
    events = read_events_strict(change)
    started = [event for event in events if event.get("type") == "task_attempt_started"]
    failures = [event for event in events if event.get("type") == "task_attempt_failed"]
    deferred = [event for event in events if event.get("type") == "task_scheduling_deferred"]
    assert started == []
    assert failures == []
    assert len(deferred) == 1
    assert deferred[0]["task_id"] == "update-issue"
    assert "attempt_id" not in deferred[0]
    assert deferred[0]["next_retry_at"] is not None
    assert locks.calls == [(("project:issue-registry", "project:zzz"), 0.125)]


def test_project_lock_timeout_is_attributed_to_task_owning_blocked_token(
    tmp_path: Path,
) -> None:
    project = _make_project(tmp_path)
    change = project / "qa/changes/CH-1"
    store = TreeStore(change)
    tree_id = store.capture(project)
    _seed_invocation(change, tree_id)

    def synchronized_task(task_id: str, prefix: str, token: str, index: int) -> ExecutableTask:
        synchronized = (ResourcePath.parse(prefix),)
        return _task(
            task_id,
            declaration_index=index,
            retry_on=["conflict"],
            retryable=("conflict",),
        ).model_copy(
            update={
                "resources": ResourceClaims(
                    reads=synchronized,
                    writes=synchronized,
                    synchronized=synchronized,
                    exclusive=(token,),
                    authorization_writes=synchronized,
                )
            }
        )

    task_a = synchronized_task("task-a", "project:qa/a/**", "project:a", 0)
    task_b = synchronized_task("task-b", "project:qa/b/**", "project:b", 1)
    locks = _TokenBlockingProjectLocks("project:b")
    runner = _ScriptedRunner(
        {
            "task-a": lambda *_: (_ for _ in ()).throw(AssertionError("task A ran")),
            "task-b": lambda *_: (_ for _ in ()).throw(AssertionError("task B ran")),
        }
    )
    scheduler = Scheduler(
        checkpoints=CheckpointStore(change),
        object_store=store,
        workspace_backend=WorkspaceBackend(change),
        node_runner=runner,
        project_lock_manager=locks,
    )

    result = scheduler.execute(_plan(task_a, task_b), _projection(change, tree_id), _context(project))

    assert result.failed == ()
    assert result.retry_at is not None
    assert runner.calls == []
    assert locks.calls == [("project:a", "project:b")]
    attempts = [
        event
        for event in read_events_strict(change)
        if event.get("type") in ("task_attempt_started", "task_attempt_failed")
    ]
    deferred = [
        event for event in read_events_strict(change) if event.get("type") == "task_scheduling_deferred"
    ]
    assert attempts == []
    assert [event["task_id"] for event in deferred] == ["task-b"]
    assert "attempt_id" not in deferred[0]


def test_synchronized_pending_update_reacquires_lock_and_replays_without_handler(
    tmp_path: Path,
) -> None:
    project = _make_project(tmp_path)
    change = project / "qa/changes/CH-1"
    issue = project / "qa/issues/ISSUE-1.json"
    issue.parent.mkdir(parents=True)
    issue.write_text('{"version":1}\n', encoding="utf-8")
    store = TreeStore(change)
    invocation_tree = store.capture(project)
    _seed_invocation(change, invocation_tree)
    issue.write_text('{"version":2}\n', encoding="utf-8")
    locks = _TrackingProjectLocks()
    calls = 0

    def update_issue(task, workspace, context) -> TaskResult:
        nonlocal calls
        calls += 1
        (workspace.project_root / "qa/issues/ISSUE-1.json").write_text('{"version":3}\n', encoding="utf-8")
        result = workspace.change_dir / "results/update.json"
        result.parent.mkdir(parents=True)
        result.write_text('{"updated":true}\n', encoding="utf-8")
        return TaskResult(status="succeeded")

    scheduler = Scheduler(
        checkpoints=CheckpointStore(change),
        object_store=store,
        workspace_backend=WorkspaceBackend(change),
        node_runner=_ScriptedRunner({"update-issue": update_issue}),
        project_lock_manager=locks,
        project_lock_timeout_seconds=0.25,
    )
    plan = _plan(_synchronized_task())
    projection = _projection(change, invocation_tree)
    commit_wave = scheduler._commit_wave

    def crash_before_update(**kwargs):
        raise RuntimeError("simulated coordinator crash before Update")

    scheduler._commit_wave = crash_before_update  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="before Update"):
        scheduler.execute(plan, projection, _context(project))
    scheduler._commit_wave = commit_wave  # type: ignore[method-assign]

    assert calls == 1
    assert issue.read_text() == '{"version":2}\n'
    live = project_invocation(change, _INV)
    scheduler._commit_wave(
        plan=plan,
        projection=live,
        context=_context(project),
        succeeded_ids=["update-issue"],
    )

    assert calls == 1
    assert issue.read_text() == '{"version":3}\n'
    assert (change / "results/update.json").read_text() == '{"updated":true}\n'
    assert locks.calls == [
        (("project:issue-registry", "project:zzz"), 0.25),
        (("project:issue-registry", "project:zzz"), 0.25),
    ]


def test_commit_pending_write_sets_replays_uncommitted_superstep(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    change = project / "qa/changes/CH-1"
    issue = project / "qa/issues/ISSUE-1.json"
    issue.parent.mkdir(parents=True)
    issue.write_text('{"version":1}\n', encoding="utf-8")
    store = TreeStore(change)
    invocation_tree = store.capture(project)
    _seed_invocation(change, invocation_tree)
    calls = 0

    def update_issue(task, workspace, context) -> TaskResult:
        nonlocal calls
        calls += 1
        (workspace.project_root / "qa/issues/ISSUE-1.json").write_text('{"version":2}\n', encoding="utf-8")
        result = workspace.change_dir / "results/update.json"
        result.parent.mkdir(parents=True)
        result.write_text('{"updated":true}\n', encoding="utf-8")
        return TaskResult(status="succeeded")

    scheduler = Scheduler(
        checkpoints=CheckpointStore(change),
        object_store=store,
        workspace_backend=WorkspaceBackend(change),
        node_runner=_ScriptedRunner({"update-issue": update_issue}),
    )
    plan = _plan(_synchronized_task())
    projection = _projection(change, invocation_tree)
    commit_wave = scheduler._commit_wave

    def crash_before_update(**kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("simulated crash before Update")

    scheduler._commit_wave = crash_before_update  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="simulated crash"):
        scheduler.execute(plan, projection, _context(project))
    scheduler._commit_wave = commit_wave  # type: ignore[method-assign]

    assert calls == 1
    live = project_invocation(change, _INV)
    committed = scheduler.commit_pending_write_sets(
        plan=plan,
        projection=live,
        context=_context(project),
        succeeded_ids=["update-issue"],
    )

    assert committed is True
    assert calls == 1
    assert issue.read_text() == '{"version":2}\n'
    assert any(event.get("type") == "superstep_committed" for event in read_events_strict(change))


def test_commit_pending_write_sets_propagates_errors(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    change = project / "qa/changes/CH-1"
    store = TreeStore(change)
    invocation_tree = store.capture(project)
    _seed_invocation(change, invocation_tree)
    scheduler = Scheduler(
        checkpoints=CheckpointStore(change),
        object_store=store,
        workspace_backend=WorkspaceBackend(change),
        node_runner=_ScriptedRunner({}),
    )
    plan = _plan(_task("only-task"))
    projection = _projection(change, invocation_tree)

    def boom(**_kwargs):  # type: ignore[no-untyped-def]
        raise ValueError("commit refused")

    scheduler._commit_wave = boom  # type: ignore[method-assign]  # noqa: SLF001
    with pytest.raises(ValueError, match="commit refused"):
        scheduler.commit_pending_write_sets(
            plan=plan,
            projection=projection,
            context=_context(project),
            succeeded_ids=["only-task"],
        )


# ---------------------------------------------------------------------------
# prepared synchronized nested waves (Task 9)


_SYNC_KNOWLEDGE_CONTRACTS = """\
schema_version: "1"
contracts:
  operation:sync-knowledge:
    handler: operation
    reads: [project:.aa/data-knowledge.yaml]
    writes: [project:.aa/data-knowledge.yaml]
    synchronized: [project:.aa/data-knowledge.yaml]
    exclusive: [project:knowledge-registry]
    authorization_writes: [project:.aa/data-knowledge.yaml]
    retryable_errors: []
  operation:inactive-knowledge:
    handler: operation
    reads: [project:.aa/inactive-knowledge.yaml]
    writes: [project:.aa/inactive-knowledge.yaml]
    synchronized: [project:.aa/inactive-knowledge.yaml]
    exclusive: [project:inactive-registry]
    authorization_writes: [project:.aa/inactive-knowledge.yaml]
    retryable_errors: []
  operation:plain:
    handler: operation
    side_effect_free: true
    retryable_errors: []
"""

_ONE_LEVEL_NESTED = """
  main:
    max_supersteps: 8
    nodes:
      child-run:
        uses: graph:leaf
        retry: never
    edges:
      - {from: START, to: child-run}
      - {from: child-run, to: END}
  leaf:
    max_supersteps: 8
    nodes:
      sync-knowledge:
        uses: operation:sync-knowledge
        retry: never
    edges:
      - {from: START, to: sync-knowledge}
      - {from: sync-knowledge, to: END}
"""

_TWO_LEVEL_NESTED = """
  main:
    max_supersteps: 8
    nodes:
      mid:
        uses: graph:mid
        retry: never
    edges:
      - {from: START, to: mid}
      - {from: mid, to: END}
  mid:
    max_supersteps: 8
    nodes:
      leaf-run:
        uses: graph:leaf
        retry: never
    edges:
      - {from: START, to: leaf-run}
      - {from: leaf-run, to: END}
  leaf:
    max_supersteps: 8
    nodes:
      sync-knowledge:
        uses: operation:sync-knowledge
        retry: never
    edges:
      - {from: START, to: sync-knowledge}
      - {from: sync-knowledge, to: END}
"""

_THREE_LEVEL_NESTED = """
  main:
    max_supersteps: 8
    nodes:
      l1:
        uses: graph:l2
        retry: never
    edges:
      - {from: START, to: l1}
      - {from: l1, to: END}
  l2:
    max_supersteps: 8
    nodes:
      l2-run:
        uses: graph:l3
        retry: never
    edges:
      - {from: START, to: l2-run}
      - {from: l2-run, to: END}
  l3:
    max_supersteps: 8
    nodes:
      l3-run:
        uses: graph:leaf
        retry: never
    edges:
      - {from: START, to: l3-run}
      - {from: l3-run, to: END}
  leaf:
    max_supersteps: 8
    nodes:
      sync-knowledge:
        uses: operation:sync-knowledge
        retry: never
    edges:
      - {from: START, to: sync-knowledge}
      - {from: sync-knowledge, to: END}
"""


def _compile_nested(body: str):
    from assurance_agent.workflow.graph.compiler import compile_workflow
    from assurance_agent.workflow.graph.schema_v2 import parse_workflow_v2

    text = (
        "name: nested-sync\n"
        "entrypoints:\n  full: {graph: main}\n"
        "policies:\n"
        "  retry:\n    never: {max_attempts: 1, retry_on: []}\n"
        "  scheduler: {max_parallel_tasks: 4}\n"
        "graphs:\n" + body
    )
    contracts = parse_execution_contracts(_SYNC_KNOWLEDGE_CONTRACTS)
    return compile_workflow(parse_workflow_v2(text), contracts), contracts


class _KnowledgeArtifacts:
    def read_json(self, tree_id: str, logical_path: str):
        raise KeyError(logical_path)


def _seed_child_invocation(
    change: Path,
    *,
    invocation_id: str,
    parent_invocation_id: str,
    entrypoint: str,
    compiled,
    tree_id: str,
    structural_path: str,
) -> None:
    append_event_strict(
        change,
        {
            "source": "graph",
            "type": "graph_invocation_started",
            "invocation_id": invocation_id,
            "entrypoint": entrypoint,
            "graph_id": entrypoint,
            "graph_digest": compiled.digest,
            "contract_digests": dict(compiled.contract_digests),
            **v6_started_bindings(),
            "params": {},
            "params_sha256": "p" * 64,
            "root_tree_id": tree_id,
            "max_parallel_tasks": 4,
            "checkpoint_ns": invocation_id,
            "parent_invocation_id": parent_invocation_id,
            "structural_path": structural_path,
        },
    )


def _prepare_nested_project(tmp_path: Path, compiled) -> tuple[Path, TreeStore, str]:
    project = tmp_path / "proj"
    change = project / "qa" / "changes" / "CH-1"
    change.mkdir(parents=True)
    aa = project / ".aa"
    aa.mkdir()
    knowledge = aa / "data-knowledge.yaml"
    knowledge.write_text("snapshot v1\n", encoding="utf-8")
    (project / "app").mkdir()
    (project / "app" / "source.py").write_text("app base\n", encoding="utf-8")
    store = TreeStore(change)
    tree_id = store.capture(project)
    _seed_invocation(change, tree_id, compiled=compiled)
    knowledge.write_text("promoted live v2\n", encoding="utf-8")
    (project / "app" / "source.py").write_text("unrelated drift\n", encoding="utf-8")
    return project, store, tree_id


def _reserve_and_execute_nested(
    tmp_path: Path,
    body: str,
    *,
    overlay_calls: list[int] | None = None,
) -> None:
    from assurance_agent.workflow.graph.checkpoint import project_invocation
    from assurance_agent.workflow.graph.selected_wave import preview_selected_wave

    compiled, contracts = _compile_nested(body)
    project, store, tree_id = _prepare_nested_project(tmp_path, compiled)
    change = project / "qa/changes/CH-1"
    projection = project_invocation(change, _INV).model_copy(
        update={
            "current_tree_id": tree_id,
            "root_tree_id": tree_id,
            "graph_digest": compiled.digest,
            "contract_digests": dict(compiled.contract_digests),
        }
    )
    context = _context(project)
    artifacts = _KnowledgeArtifacts()
    child_projections: dict[str, GraphProjection] = {}

    def _child_projection(invocation_id: str) -> GraphProjection:
        return project_invocation(change, invocation_id).model_copy(
            update={
                "graph_digest": compiled.digest,
                "contract_digests": dict(compiled.contract_digests),
            }
        )

    if "graph:leaf" in body or "graph:mid" in body:
        from assurance_agent.workflow.graph.planner import plan_superstep
        from assurance_agent.workflow.graph.selected_wave import derive_child_invocation_id

        parent_tasks = {t.node_id: t for t in plan_superstep(compiled, projection, context, artifacts).tasks}
        if "child-run" in parent_tasks:
            child_id = derive_child_invocation_id(parent_tasks["child-run"], "leaf")
            _seed_child_invocation(
                change,
                invocation_id=child_id,
                parent_invocation_id=_INV,
                entrypoint="leaf",
                compiled=compiled,
                tree_id=tree_id,
                structural_path="main/child-run/leaf",
            )
            child_projections[child_id] = _child_projection(child_id)
        if "mid" in parent_tasks:
            mid_id = derive_child_invocation_id(parent_tasks["mid"], "mid")
            _seed_child_invocation(
                change,
                invocation_id=mid_id,
                parent_invocation_id=_INV,
                entrypoint="mid",
                compiled=compiled,
                tree_id=tree_id,
                structural_path="main/mid/mid",
            )
            child_projections[mid_id] = _child_projection(mid_id)
            mid_tasks = {
                t.node_id: t
                for t in plan_superstep(compiled, child_projections[mid_id], context, artifacts).tasks
            }
            if "leaf-run" in mid_tasks:
                leaf_id = derive_child_invocation_id(mid_tasks["leaf-run"], "leaf")
                _seed_child_invocation(
                    change,
                    invocation_id=leaf_id,
                    parent_invocation_id=mid_id,
                    entrypoint="leaf",
                    compiled=compiled,
                    tree_id=tree_id,
                    structural_path="main/mid/mid/leaf-run/leaf",
                )
                child_projections[leaf_id] = _child_projection(leaf_id)
        if "l1" in parent_tasks:
            l2_id = derive_child_invocation_id(parent_tasks["l1"], "l2")
            _seed_child_invocation(
                change,
                invocation_id=l2_id,
                parent_invocation_id=_INV,
                entrypoint="l2",
                compiled=compiled,
                tree_id=tree_id,
                structural_path="main/l1/l2",
            )
            child_projections[l2_id] = _child_projection(l2_id)
            l2_tasks = {
                t.node_id: t
                for t in plan_superstep(compiled, child_projections[l2_id], context, artifacts).tasks
            }
            l3_id = derive_child_invocation_id(l2_tasks["l2-run"], "l3")
            _seed_child_invocation(
                change,
                invocation_id=l3_id,
                parent_invocation_id=l2_id,
                entrypoint="l3",
                compiled=compiled,
                tree_id=tree_id,
                structural_path="main/l1/l2/l2-run/l3",
            )
            child_projections[l3_id] = _child_projection(l3_id)
            l3_tasks = {
                t.node_id: t
                for t in plan_superstep(compiled, child_projections[l3_id], context, artifacts).tasks
            }
            leaf_id = derive_child_invocation_id(l3_tasks["l3-run"], "leaf")
            _seed_child_invocation(
                change,
                invocation_id=leaf_id,
                parent_invocation_id=l3_id,
                entrypoint="leaf",
                compiled=compiled,
                tree_id=tree_id,
                structural_path="main/l1/l2/l2-run/l3/l3-run/leaf",
            )
            child_projections[leaf_id] = project_invocation(change, leaf_id)

    selected = preview_selected_wave(
        compiled,
        projection,
        context,
        artifacts,
        max_parallel_tasks=4,
        child_projections=child_projections,
    )
    assert selected is not None

    read_count = 0
    knowledge = project / ".aa/data-knowledge.yaml"
    real_read_bytes = Path.read_bytes

    def counting_read(self: Path) -> bytes:
        nonlocal read_count
        if self.resolve() == knowledge.resolve():
            read_count += 1
        return real_read_bytes(self)

    overlay_many = store.overlay_synchronized_paths_many

    def observed_overlay_many(tree_ids, project_root, paths):
        if overlay_calls is not None:
            overlay_calls.append(len(tree_ids))
        return overlay_many(tree_ids, project_root, paths)

    store.overlay_synchronized_paths_many = observed_overlay_many  # type: ignore[method-assign]

    locks = _TrackingProjectLocks()
    scheduler = Scheduler(
        checkpoints=CheckpointStore(change),
        object_store=store,
        workspace_backend=WorkspaceBackend(change),
        node_runner=_ScriptedRunner({}),
        project_lock_manager=locks,
        contracts=contracts,
    )

    import pytest as pytest_mod

    monkeypatch = pytest_mod.MonkeyPatch()
    monkeypatch.setattr(Path, "read_bytes", counting_read)
    try:
        locked, lease = scheduler.reserve_selected_wave(
            selected,
            context,
            compiled=compiled,
            artifacts=artifacts,
            child_projections=child_projections,
        )
    finally:
        monkeypatch.undo()

    assert read_count == 1
    if overlay_calls is not None:
        assert overlay_calls == [1]
    assert lease.synchronized_paths
    assert lease.capture_sealed is True


def test_prepared_direct_leaf_reads_promoted_digest(tmp_path: Path) -> None:
    from assurance_agent.workflow.graph.checkpoint import project_invocation
    from assurance_agent.workflow.graph.selected_wave import preview_selected_wave

    linear = """
  main:
    max_supersteps: 8
    nodes:
      sync-knowledge:
        uses: operation:sync-knowledge
        retry: never
    edges:
      - {from: START, to: sync-knowledge}
      - {from: sync-knowledge, to: END}
"""
    compiled, contracts = _compile_nested(linear)
    project, store, tree_id = _prepare_nested_project(tmp_path, compiled)
    change = project / "qa/changes/CH-1"
    projection = project_invocation(change, _INV).model_copy(
        update={
            "current_tree_id": tree_id,
            "root_tree_id": tree_id,
            "graph_digest": compiled.digest,
            "contract_digests": dict(compiled.contract_digests),
        }
    )
    context = _context(project)
    artifacts = _KnowledgeArtifacts()
    selected = preview_selected_wave(
        compiled, projection, context, artifacts, max_parallel_tasks=4, child_projections={}
    )
    assert selected is not None
    task_id = selected.selected_tasks[0].task_id
    observed: list[bytes] = []

    def sync_knowledge(task, workspace, ctx) -> TaskResult:
        observed.append((workspace.project_root / ".aa/data-knowledge.yaml").read_bytes())
        assert (workspace.project_root / "app/source.py").read_text() == "app base\n"
        return TaskResult(status="succeeded")

    scheduler = Scheduler(
        checkpoints=CheckpointStore(change),
        object_store=store,
        workspace_backend=WorkspaceBackend(change),
        node_runner=_ScriptedRunner({task_id: sync_knowledge}),
        project_lock_manager=_TrackingProjectLocks(),
        contracts=contracts,
    )
    locked, lease = scheduler.reserve_selected_wave(
        selected, context, compiled=compiled, artifacts=artifacts, child_projections={}
    )
    result = scheduler.execute_selected_wave(lease, locked, invocation_id=_INV)
    assert result.succeeded == (task_id,)
    assert observed == [b"promoted live v2\n"]


def test_prepared_wave_one_level_nested_single_capture(tmp_path: Path) -> None:
    overlay_calls: list[int] = []
    _reserve_and_execute_nested(tmp_path, _ONE_LEVEL_NESTED, overlay_calls=overlay_calls)


def test_prepared_wave_two_level_nested_single_capture(tmp_path: Path) -> None:
    overlay_calls: list[int] = []
    _reserve_and_execute_nested(tmp_path, _TWO_LEVEL_NESTED, overlay_calls=overlay_calls)


def test_prepared_wave_three_level_nested_single_capture(tmp_path: Path) -> None:
    overlay_calls: list[int] = []
    _reserve_and_execute_nested(tmp_path, _THREE_LEVEL_NESTED, overlay_calls=overlay_calls)


def test_reserve_selected_wave_fails_on_identity_mutation(tmp_path: Path) -> None:
    from assurance_agent.workflow.graph.checkpoint import project_invocation
    from assurance_agent.workflow.graph.selected_wave import SelectedWaveDriftError, preview_selected_wave

    linear = """
  main:
    max_supersteps: 8
    nodes:
      sync-knowledge:
        uses: operation:sync-knowledge
        retry: never
    edges:
      - {from: START, to: sync-knowledge}
      - {from: sync-knowledge, to: END}
"""
    compiled, contracts = _compile_nested(linear)
    project, store, tree_id = _prepare_nested_project(tmp_path, compiled)
    change = project / "qa/changes/CH-1"
    projection = project_invocation(change, _INV).model_copy(
        update={
            "current_tree_id": tree_id,
            "root_tree_id": tree_id,
            "graph_digest": compiled.digest,
            "contract_digests": dict(compiled.contract_digests),
        }
    )
    context = _context(project)
    artifacts = _KnowledgeArtifacts()
    selected = preview_selected_wave(
        compiled, projection, context, artifacts, max_parallel_tasks=4, child_projections={}
    )
    assert selected is not None
    scheduler = Scheduler(
        checkpoints=CheckpointStore(change),
        object_store=store,
        workspace_backend=WorkspaceBackend(change),
        node_runner=_ScriptedRunner({}),
        project_lock_manager=_TrackingProjectLocks(),
        contracts=contracts,
    )
    locked, lease = scheduler.reserve_selected_wave(
        selected, context, compiled=compiled, artifacts=artifacts, child_projections={}
    )
    from dataclasses import replace

    from assurance_agent.workflow.graph.compiler import canonical_digest

    mutated_task = selected.selected_tasks[0].model_copy(update={"target": "operation:plain"})
    mutated = replace(
        selected,
        selected_tasks=(mutated_task,),
        identity_digest=canonical_digest({"mutated": True}),
    )
    with pytest.raises(SelectedWaveDriftError):
        scheduler.reserve_selected_wave(
            mutated,
            locked,
            compiled=compiled,
            artifacts=artifacts,
            child_projections={},
            inherited_lease=lease,
        )
    started = [e for e in read_events_strict(change) if e.get("type") == "task_attempt_started"]
    assert started == []


def test_prepared_wave_holds_project_locks_during_handler(tmp_path: Path) -> None:
    from assurance_agent.workflow.graph.checkpoint import project_invocation
    from assurance_agent.workflow.graph.planner import plan_superstep
    from assurance_agent.workflow.graph.selected_wave import preview_selected_wave

    linear = """
  main:
    max_supersteps: 8
    nodes:
      sync-knowledge:
        uses: operation:sync-knowledge
        retry: never
    edges:
      - {from: START, to: sync-knowledge}
      - {from: sync-knowledge, to: END}
"""
    compiled, contracts = _compile_nested(linear)
    project, store, tree_id = _prepare_nested_project(tmp_path, compiled)
    change = project / "qa/changes/CH-1"
    projection = project_invocation(change, _INV).model_copy(
        update={
            "current_tree_id": tree_id,
            "root_tree_id": tree_id,
            "graph_digest": compiled.digest,
            "contract_digests": dict(compiled.contract_digests),
        }
    )
    context = _context(project)
    artifacts = _KnowledgeArtifacts()
    selected = preview_selected_wave(
        compiled, projection, context, artifacts, max_parallel_tasks=4, child_projections={}
    )
    assert selected is not None
    plan = plan_superstep(compiled, projection, context, artifacts)
    task_id = selected.selected_tasks[0].task_id
    locks = _TrackingProjectLocks()
    phases: list[str] = []

    overlay_many = store.overlay_synchronized_paths_many

    def observed_overlay_many(tree_ids, project_root, paths):
        assert locks.held
        phases.append("overlay")
        return overlay_many(tree_ids, project_root, paths)

    apply_targeted = store.apply_write_sets_to_synchronized_paths

    def observed_apply(project_root, write_sets, paths):
        assert locks.held
        phases.append("apply")
        return apply_targeted(project_root, write_sets, paths)

    store.overlay_synchronized_paths_many = observed_overlay_many  # type: ignore[method-assign]
    store.apply_write_sets_to_synchronized_paths = observed_apply  # type: ignore[method-assign]

    def sync_knowledge(task, workspace, ctx) -> TaskResult:
        assert locks.held
        phases.append("handler")
        return TaskResult(status="succeeded")

    scheduler = Scheduler(
        checkpoints=CheckpointStore(change),
        object_store=store,
        workspace_backend=WorkspaceBackend(change),
        node_runner=_ScriptedRunner({task_id: sync_knowledge}),
        project_lock_manager=locks,
        contracts=contracts,
    )
    result = scheduler.execute(
        plan,
        projection,
        context,
        selected_wave=selected,
        compiled=compiled,
        artifacts=artifacts,
        child_projections={},
    )

    assert result.succeeded == (task_id,)
    assert locks.calls == [(("project:knowledge-registry",), 5.0)]
    assert phases == ["overlay", "handler", "apply"]
    assert locks.held is False


def test_execute_selected_wave_fails_on_task_mutation_after_reserve(tmp_path: Path) -> None:
    from assurance_agent.workflow.graph import selected_wave as selected_wave_mod
    from assurance_agent.workflow.graph.checkpoint import project_invocation
    from assurance_agent.workflow.graph.compiler import canonical_digest
    from assurance_agent.workflow.graph.selected_wave import SelectedWaveDriftError, preview_selected_wave

    linear = """
  main:
    max_supersteps: 8
    nodes:
      sync-knowledge:
        uses: operation:sync-knowledge
        retry: never
    edges:
      - {from: START, to: sync-knowledge}
      - {from: sync-knowledge, to: END}
"""
    compiled, contracts = _compile_nested(linear)
    project, store, tree_id = _prepare_nested_project(tmp_path, compiled)
    change = project / "qa/changes/CH-1"
    projection = project_invocation(change, _INV).model_copy(
        update={
            "current_tree_id": tree_id,
            "root_tree_id": tree_id,
            "graph_digest": compiled.digest,
            "contract_digests": dict(compiled.contract_digests),
        }
    )
    context = _context(project)
    artifacts = _KnowledgeArtifacts()
    selected = preview_selected_wave(
        compiled, projection, context, artifacts, max_parallel_tasks=4, child_projections={}
    )
    assert selected is not None
    scheduler = Scheduler(
        checkpoints=CheckpointStore(change),
        object_store=store,
        workspace_backend=WorkspaceBackend(change),
        node_runner=_ScriptedRunner(
            {selected.selected_tasks[0].task_id: lambda *_: TaskResult(status="succeeded")}
        ),
        project_lock_manager=_TrackingProjectLocks(),
        contracts=contracts,
    )
    locked, lease = scheduler.reserve_selected_wave(
        selected, context, compiled=compiled, artifacts=artifacts, child_projections={}
    )

    original_preview = selected_wave_mod.preview_selected_wave
    reserve_done = {"done": True}

    def drifted_preview(*args, **kwargs):
        wave = original_preview(*args, **kwargs)
        if reserve_done["done"] and wave is not None:
            mutated_task = wave.selected_tasks[0].model_copy(update={"target": "operation:plain"})
            from dataclasses import replace

            return replace(
                wave,
                selected_tasks=(mutated_task,),
                identity_digest=canonical_digest({"mutated": True}),
            )
        return wave

    import pytest as pytest_mod

    monkeypatch = pytest_mod.MonkeyPatch()
    monkeypatch.setattr(selected_wave_mod, "preview_selected_wave", drifted_preview)
    try:
        with pytest.raises(SelectedWaveDriftError):
            scheduler.execute_selected_wave(
                lease,
                locked,
                invocation_id=_INV,
                compiled=compiled,
                artifacts=artifacts,
                child_projections={},
            )
    finally:
        monkeypatch.undo()

    started = [e for e in read_events_strict(change) if e.get("type") == "task_attempt_started"]
    assert started == []


def test_execute_selected_wave_fails_on_child_plan_change_after_reserve(tmp_path: Path) -> None:
    from assurance_agent.workflow.graph.checkpoint import project_invocation
    from assurance_agent.workflow.graph.planner import plan_superstep
    from assurance_agent.workflow.graph.selected_wave import (
        SelectedWaveDriftError,
        derive_child_invocation_id,
        preview_selected_wave,
    )

    compiled, contracts = _compile_nested(_ONE_LEVEL_NESTED)
    project, store, tree_id = _prepare_nested_project(tmp_path, compiled)
    change = project / "qa/changes/CH-1"
    projection = project_invocation(change, _INV).model_copy(
        update={
            "current_tree_id": tree_id,
            "root_tree_id": tree_id,
            "graph_digest": compiled.digest,
            "contract_digests": dict(compiled.contract_digests),
        }
    )
    context = _context(project)
    artifacts = _KnowledgeArtifacts()
    parent_tasks = {t.node_id: t for t in plan_superstep(compiled, projection, context, artifacts).tasks}
    child_id = derive_child_invocation_id(parent_tasks["child-run"], "leaf")
    _seed_child_invocation(
        change,
        invocation_id=child_id,
        parent_invocation_id=_INV,
        entrypoint="leaf",
        compiled=compiled,
        tree_id=tree_id,
        structural_path="main/child-run/leaf",
    )
    child_projections = {
        child_id: project_invocation(change, child_id).model_copy(
            update={
                "graph_digest": compiled.digest,
                "contract_digests": dict(compiled.contract_digests),
            }
        )
    }
    selected = preview_selected_wave(
        compiled,
        projection,
        context,
        artifacts,
        max_parallel_tasks=4,
        child_projections=child_projections,
    )
    assert selected is not None
    child_task_id = selected.child_waves[0].selected_tasks[0].task_id
    scheduler = Scheduler(
        checkpoints=CheckpointStore(change),
        object_store=store,
        workspace_backend=WorkspaceBackend(change),
        node_runner=_ScriptedRunner(
            {
                parent_tasks["child-run"].task_id: lambda *_: TaskResult(status="succeeded"),
                child_task_id: lambda *_: TaskResult(status="succeeded"),
            }
        ),
        project_lock_manager=_TrackingProjectLocks(),
        contracts=contracts,
    )
    locked, lease = scheduler.reserve_selected_wave(
        selected,
        context,
        compiled=compiled,
        artifacts=artifacts,
        child_projections=child_projections,
    )

    from assurance_agent.workflow.graph import selected_wave as selected_wave_mod

    original_preview = selected_wave_mod.preview_selected_wave
    reserve_done = {"done": True}

    def drifted_child_preview(*args, **kwargs):
        wave = original_preview(*args, **kwargs)
        if reserve_done["done"] and wave is not None and wave.invocation_id == child_id:
            from dataclasses import replace

            from assurance_agent.workflow.graph.compiler import canonical_digest

            return replace(
                wave,
                selected_tasks=(),
                identity_digest=canonical_digest({"child-plan-changed": True}),
            )
        return wave

    import pytest as pytest_mod

    monkeypatch = pytest_mod.MonkeyPatch()
    monkeypatch.setattr(selected_wave_mod, "preview_selected_wave", drifted_child_preview)
    try:
        with pytest.raises(SelectedWaveDriftError):
            scheduler.execute_selected_wave(
                lease,
                locked,
                invocation_id=_INV,
                compiled=compiled,
                artifacts=artifacts,
                child_projections=child_projections,
            )
    finally:
        monkeypatch.undo()
    started = [e for e in read_events_strict(change) if e.get("type") == "task_attempt_started"]
    assert started == []


def test_execute_selected_wave_fails_on_inactive_branch_synchronized_path_drift(
    tmp_path: Path,
) -> None:
    from assurance_agent.workflow.graph import selected_wave as selected_wave_mod
    from assurance_agent.workflow.graph.checkpoint import project_invocation
    from assurance_agent.workflow.graph.contracts import ResourcePath
    from assurance_agent.workflow.graph.selected_wave import SelectedWaveDriftError, preview_selected_wave

    nested = """
  main:
    max_supersteps: 8
    nodes:
      bootstrap:
        uses: graph:child
        retry: never
    edges:
      - {from: START, to: bootstrap}
      - {from: bootstrap, to: END}
  child:
    max_supersteps: 8
    nodes:
      active-sync:
        uses: operation:sync-knowledge
        retry: never
      dormant-sync:
        uses: operation:inactive-knowledge
        retry: never
    edges:
      - {from: START, to: active-sync}
      - {from: START, to: dormant-sync, when: "false"}
      - {from: active-sync, to: END}
      - {from: dormant-sync, to: END}
"""
    compiled, contracts = _compile_nested(nested)
    project, store, tree_id = _prepare_nested_project(tmp_path, compiled)
    change = project / "qa/changes/CH-1"
    projection = project_invocation(change, _INV).model_copy(
        update={
            "current_tree_id": tree_id,
            "root_tree_id": tree_id,
            "graph_digest": compiled.digest,
            "contract_digests": dict(compiled.contract_digests),
        }
    )
    context = _context(project)
    artifacts = _KnowledgeArtifacts()
    selected = preview_selected_wave(
        compiled, projection, context, artifacts, max_parallel_tasks=4, child_projections={}
    )
    assert selected is not None
    scheduler = Scheduler(
        checkpoints=CheckpointStore(change),
        object_store=store,
        workspace_backend=WorkspaceBackend(change),
        node_runner=_ScriptedRunner({}),
        project_lock_manager=_TrackingProjectLocks(),
        contracts=contracts,
    )
    locked, lease = scheduler.reserve_selected_wave(
        selected, context, compiled=compiled, artifacts=artifacts, child_projections={}
    )

    inactive_path = ResourcePath.parse("project:.aa/inactive-knowledge.yaml")
    original_preview = selected_wave_mod.preview_selected_wave
    reserve_done = {"done": True}

    def drifted_preview(*args, **kwargs):
        wave = original_preview(*args, **kwargs)
        if reserve_done["done"] and wave is not None and wave.synchronized_paths == ():
            from dataclasses import replace

            return replace(
                wave,
                synchronized_paths=(inactive_path,),
                identity_digest=wave.identity_digest + "-drift",
            )
        return wave

    import pytest as pytest_mod

    monkeypatch = pytest_mod.MonkeyPatch()
    monkeypatch.setattr(selected_wave_mod, "preview_selected_wave", drifted_preview)
    try:
        with pytest.raises(SelectedWaveDriftError):
            scheduler.execute_selected_wave(
                lease,
                locked,
                invocation_id=_INV,
                compiled=compiled,
                artifacts=artifacts,
                child_projections={},
            )
    finally:
        monkeypatch.undo()

    started = [e for e in read_events_strict(change) if e.get("type") == "task_attempt_started"]
    assert started == []


def test_deferred_child_outer_wave_reserves_footprint_locks_before_run_child(
    tmp_path: Path,
) -> None:
    """Outer graph:* with footprint locks but no child uses prepared path, not legacy execute."""
    from assurance_agent.workflow.graph.checkpoint import project_invocation
    from assurance_agent.workflow.graph.planner import plan_superstep
    from assurance_agent.workflow.graph.selected_wave import (
        derive_child_invocation_id,
        preview_selected_wave,
    )

    nested = """
  main:
    max_supersteps: 8
    nodes:
      bootstrap:
        uses: graph:child
        retry: never
    edges:
      - {from: START, to: bootstrap}
      - {from: bootstrap, to: END}
  child:
    max_supersteps: 8
    nodes:
      active-sync:
        uses: operation:sync-knowledge
        retry: never
      dormant-sync:
        uses: operation:inactive-knowledge
        retry: never
    edges:
      - {from: START, to: active-sync}
      - {from: START, to: dormant-sync, when: "false"}
      - {from: active-sync, to: END}
      - {from: dormant-sync, to: END}
"""
    compiled, contracts = _compile_nested(nested)
    project, store, tree_id = _prepare_nested_project(tmp_path, compiled)
    change = project / "qa/changes/CH-1"
    projection = project_invocation(change, _INV).model_copy(
        update={
            "current_tree_id": tree_id,
            "root_tree_id": tree_id,
            "graph_digest": compiled.digest,
            "contract_digests": dict(compiled.contract_digests),
        }
    )
    context = _context(project)
    artifacts = _KnowledgeArtifacts()
    selected = preview_selected_wave(
        compiled, projection, context, artifacts, max_parallel_tasks=4, child_projections={}
    )
    assert selected is not None
    assert selected.lock_tokens == ("project:inactive-registry", "project:knowledge-registry")
    assert selected.synchronized_paths == ()
    assert selected.child_waves == ()

    bootstrap = selected.selected_tasks[0]
    locks = _TrackingProjectLocks()
    overlay_calls: list[int] = []
    overlay_many = store.overlay_synchronized_paths_many

    def observed_overlay_many(tree_ids, project_root, paths):
        overlay_calls.append(len(tree_ids))
        return overlay_many(tree_ids, project_root, paths)

    store.overlay_synchronized_paths_many = observed_overlay_many  # type: ignore[method-assign]

    def bootstrap_handler(task, workspace, ctx) -> TaskResult:
        assert locks.held
        assert tuple(sorted(locks.calls[-1][0])) == tuple(sorted(selected.lock_tokens))
        return TaskResult(status="succeeded")

    scheduler = Scheduler(
        checkpoints=CheckpointStore(change),
        object_store=store,
        workspace_backend=WorkspaceBackend(change),
        node_runner=_ScriptedRunner({bootstrap.task_id: bootstrap_handler}),
        project_lock_manager=locks,
        contracts=contracts,
    )

    def failing_select(*args, **kwargs):
        raise AssertionError("legacy execute must not run when selected_wave carries footprint lock_tokens")

    scheduler.select = failing_select  # type: ignore[method-assign]

    captured_leases: list[PreparedWaveLease] = []
    original_finalize = scheduler._finalize_selected_wave_reservation  # noqa: SLF001

    def spy_finalize(*args, **kwargs):
        locked_context, lease = original_finalize(*args, **kwargs)
        captured_leases.append(lease)
        return locked_context, lease

    scheduler._finalize_selected_wave_reservation = spy_finalize  # type: ignore[method-assign]

    plan = plan_superstep(compiled, projection, context, artifacts)
    result = scheduler.execute(
        plan,
        projection,
        context,
        selected_wave=selected,
        compiled=compiled,
        artifacts=artifacts,
        child_projections={},
    )
    assert result.succeeded == (bootstrap.task_id,)
    assert len(captured_leases) == 1
    assert captured_leases[0].capture_sealed is False
    lease = captured_leases[0]

    parent_tasks = {t.node_id: t for t in plan.tasks}
    child_id = derive_child_invocation_id(parent_tasks["bootstrap"], "child")
    _seed_child_invocation(
        change,
        invocation_id=child_id,
        parent_invocation_id=_INV,
        entrypoint="child",
        compiled=compiled,
        tree_id=tree_id,
        structural_path="main/bootstrap/child",
    )
    child_projections = {
        child_id: project_invocation(change, child_id).model_copy(
            update={
                "graph_digest": compiled.digest,
                "contract_digests": dict(compiled.contract_digests),
            }
        )
    }
    child_selected = preview_selected_wave(
        compiled,
        child_projections[child_id],
        context,
        artifacts,
        max_parallel_tasks=4,
        child_projections={},
    )
    assert child_selected is not None
    child_task_id = child_selected.selected_tasks[0].task_id

    def sync_knowledge(task, workspace, ctx) -> TaskResult:
        return TaskResult(status="succeeded")

    scheduler._runner = _ScriptedRunner(  # noqa: SLF001
        {
            bootstrap.task_id: bootstrap_handler,
            child_task_id: sync_knowledge,
        }
    )
    child_overlay_before = len(overlay_calls)
    locked_child, child_lease = scheduler.reserve_selected_wave(
        child_selected,
        context,
        compiled=compiled,
        artifacts=artifacts,
        child_projections={},
        inherited_lease=lease,
    )
    assert child_lease.capture_sealed is True
    assert len(overlay_calls) == child_overlay_before + 1

    child_result = scheduler.execute_selected_wave(
        child_lease,
        locked_child,
        invocation_id=child_id,
        compiled=compiled,
        artifacts=artifacts,
        child_projections={},
    )
    assert child_result.succeeded == (child_task_id,)


def test_current_change_repo_path_prefers_write_set_roots_for_child_context(tmp_path: Path) -> None:
    """Child contexts bind project_root to a task workspace; host change_dir must still resolve."""
    from assurance_agent.workflow.graph.attempt_engine import _current_change_repo_path
    from assurance_agent.workflow.graph.workspace import WriteEntry, WriteSet

    host_project = tmp_path / "sut"
    host_change = host_project / "qa" / "changes" / "CH-1"
    task_workspace = host_change / ".graph-runtime" / "tasks" / "child"
    host_change.mkdir(parents=True)
    task_workspace.mkdir(parents=True)

    context = RuntimeContext(
        project_root=task_workspace,  # child binding
        repo_root=task_workspace,
        change_dir=host_change,  # host ledger
        change_id="CH-1",
    )
    write_set = WriteSet(
        write_set_id="a" * 64,
        task_id="task-1",
        base_tree_id="b" * 64,
        entries=(
            WriteEntry(
                logical_path="change:codegen/api-codegen-summary.md",
                operation="add",
                before_sha256=None,
                after_sha256="c" * 64,
                blob_sha256="c" * 64,
            ),
        ),
        outputs_sha256={"change:codegen/api-codegen-summary.md": "c" * 64},
        base_tree_roots={"change": "qa/changes/CH-1", "project": ".", "repo": "."},
    )
    assert _current_change_repo_path(context, write_set) == "qa/changes/CH-1"

    # Without pinned roots, fall back without raising on host/workspace skew.
    bare = write_set.model_copy(update={"base_tree_roots": None})
    assert _current_change_repo_path(context, bare) == "qa/changes/CH-1"
