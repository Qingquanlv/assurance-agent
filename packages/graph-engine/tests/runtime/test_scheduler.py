from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.plugin_api import (
    CandidateWriteSet,
    CapabilityRegistry,
    ResourceClaims,
    TaskContext,
    TaskOutcome,
    TaskRequest,
    ValidationContext,
    ValidationResult,
)
from graph_engine.runtime.events import (
    EventEnvelope,
    GraphFailed,
    GraphStarted,
    HeadAdvanced,
    InvocationStarted,
    NodeActivated,
    TaskAttemptFailed,
    TaskAttemptStarted,
    TaskAttemptSucceeded,
    TaskLeaseAcquired,
    TaskLeaseHeartbeat,
)
from graph_engine.runtime.ledger import Ledger, LedgerConflictError
from graph_engine.runtime.models import PlannedTask, ProjectionError, fold_events
from graph_engine.runtime.scheduler import FakeClock, Lease, Scheduler, select_wave
from graph_engine.runtime.workspace import SnapshotStore, WorkspaceViolation


Handler = Callable[[TaskRequest, TaskContext], object]


def _task(
    name: str,
    *,
    rank: int = 0,
    index: int = 0,
    resources: ResourceClaims | None = None,
    timeout: float = 1.0,
    validators: tuple[str, ...] = (),
) -> PlannedTask:
    return PlannedTask(
        invocation_id="inv-1",
        task_id=f"task-{name}",
        activation_id=f"activation-{name}",
        graph_instance_id="graph-1",
        node_id=name,
        capability_id=f"test.tasks.{name}",
        attempt=1,
        input={"name": name},
        timeout_seconds=timeout,
        resources=resources or ResourceClaims(),
        validators=validators,
        topology_rank=rank,
        declaration_index=index,
    )


def _scheduler(
    tmp_path: Path,
    handlers: dict[str, object],
    *,
    initial: dict[str, bytes] | None = None,
    validators: dict[str, object] | None = None,
    clock: FakeClock | None = None,
    max_parallel: int = 4,
) -> tuple[Scheduler, SnapshotStore, Ledger]:
    store = SnapshotStore.create(tmp_path / "store", initial or {})
    ledger = Ledger(tmp_path / "ledger")
    scheduler = Scheduler(
        CapabilityRegistry(
            task_handlers=handlers,  # type: ignore[arg-type]
            commit_validators=validators or {},  # type: ignore[arg-type]
        ),
        store,
        ledger,
        owner_id="worker-1",
        clock=clock or FakeClock(100.0),
        lease_seconds=10.0,
        max_parallel=max_parallel,
    )
    return scheduler, store, ledger


def test_wave_uses_stable_topology_order_and_segment_aware_conflicts() -> None:
    first = _task("first", rank=0, index=1, resources=ResourceClaims(writes=("a/b",)))
    second = _task("second", rank=1, index=0, resources=ResourceClaims(reads=("a/bb",)))
    conflict = _task("conflict", rank=2, index=0, resources=ResourceClaims(reads=("a/b/c",)))

    selected = select_wave((conflict, second, first), max_parallel=4)

    assert [task.task_id for task in selected] == ["task-first", "task-second"]


@pytest.mark.parametrize(
    ("left", "right", "conflicts"),
    [
        (ResourceClaims(reads=("x",)), ResourceClaims(reads=("x/y",)), False),
        (ResourceClaims(writes=("x",)), ResourceClaims(reads=("x/y",)), True),
        (ResourceClaims(reads=("x",)), ResourceClaims(writes=("x/y",)), True),
        (ResourceClaims(writes=("x",)), ResourceClaims(writes=("x/y",)), True),
        (ResourceClaims(exclusive=("x",)), ResourceClaims(reads=("x/y",)), True),
        (ResourceClaims(exclusive=("x",)), ResourceClaims(writes=("x/y",)), True),
        (ResourceClaims(exclusive=("x",)), ResourceClaims(exclusive=("x/y",)), True),
        (ResourceClaims(exclusive=("x",)), ResourceClaims(writes=("elsewhere",)), True),
    ],
)
def test_wave_conflict_matrix(left: ResourceClaims, right: ResourceClaims, conflicts: bool) -> None:
    selected = select_wave(
        (_task("left", index=0, resources=left), _task("right", index=1, resources=right)),
        max_parallel=2,
    )
    assert len(selected) == (1 if conflicts else 2)


@pytest.mark.parametrize("value", [0, -1, True])
def test_wave_rejects_invalid_parallel_limit(value: int) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        select_wave((), value)


def test_wave_respects_parallel_limit_and_skips_conflicts() -> None:
    tasks = tuple(_task(str(index), index=index) for index in range(4))
    assert select_wave(tasks, 2) == tasks[:2]


def test_wave_stops_at_first_conflict_in_stable_order() -> None:
    first = _task("first", index=0, resources=ResourceClaims(writes=("x",)))
    conflict = _task("conflict", index=1, resources=ResourceClaims(reads=("x",)))
    later = _task("later", index=2, resources=ResourceClaims(writes=("unrelated",)))
    assert select_wave((later, conflict, first), 3) == (first,)


@pytest.mark.parametrize(
    ("raised", "kind", "message"),
    [
        (RuntimeError("broken"), "internal", "RuntimeError: broken"),
        (asyncio.CancelledError("cancelled"), "internal", "CancelledError: cancelled"),
    ],
)
def test_handler_exception_and_cancellation_are_closed_failures(
    tmp_path: Path, raised: BaseException, kind: str, message: str
) -> None:
    async def handler(_request: TaskRequest, _context: TaskContext) -> TaskOutcome:
        raise raised

    task = _task("fail", resources=ResourceClaims(writes=("out",)))
    scheduler, store, ledger = _scheduler(tmp_path, {task.capability_id: handler})
    before = store.head_tree_id()

    result = asyncio.run(scheduler.run_wave((task,)))[0]

    assert result.outcome.failure is not None
    assert result.outcome.failure.kind == kind
    assert message in result.outcome.failure.message
    assert store.head_tree_id() == before
    assert [item.event.kind for item in ledger.read_all()][-1] == "task_attempt_failed"


def test_timeout_is_typed_failure(tmp_path: Path) -> None:
    async def handler(_request: TaskRequest, _context: TaskContext) -> TaskOutcome:
        await asyncio.sleep(0.05)
        return TaskOutcome.succeeded()

    task = _task("timed", timeout=0.001)
    scheduler, _store, _ledger = _scheduler(tmp_path, {task.capability_id: handler})

    result = asyncio.run(scheduler.run_wave((task,)))[0]

    assert result.outcome.failure is not None
    assert result.outcome.failure.kind == "timeout"


def test_all_handlers_share_baseline_and_no_success_is_persisted_before_gather(
    tmp_path: Path,
) -> None:
    observed: list[bytes] = []
    slow_started = asyncio.Event()
    release = asyncio.Event()
    tasks = (
        _task("fast", index=0, resources=ResourceClaims(writes=("fast.txt",))),
        _task("slow", index=1, resources=ResourceClaims(writes=("slow.txt",))),
    )
    ledger_ref: list[Ledger] = []

    async def fast(_request: TaskRequest, context: TaskContext) -> TaskOutcome:
        observed.append((context.workspace_root / "seed.txt").read_bytes())
        (context.workspace_root / "fast.txt").write_bytes(b"fast")
        await slow_started.wait()
        assert all(item.event.kind != "task_attempt_succeeded" for item in ledger_ref[0].read_all())
        release.set()
        return TaskOutcome.succeeded({"ok": True})

    async def slow(_request: TaskRequest, context: TaskContext) -> TaskOutcome:
        observed.append((context.workspace_root / "seed.txt").read_bytes())
        slow_started.set()
        await release.wait()
        (context.workspace_root / "slow.txt").write_bytes(b"slow")
        return TaskOutcome.succeeded({"ok": True})

    scheduler, store, ledger = _scheduler(
        tmp_path,
        {tasks[0].capability_id: fast, tasks[1].capability_id: slow},
        initial={"seed.txt": b"baseline"},
    )
    ledger_ref.append(ledger)

    results = asyncio.run(scheduler.run_wave(tuple(reversed(tasks))))

    assert observed == [b"baseline", b"baseline"]
    assert [result.task.task_id for result in results] == ["task-fast", "task-slow"]
    assert store.read_head("fast.txt") == b"fast"
    assert store.read_head("slow.txt") == b"slow"


def _run_duration_scenario(root: Path, delays: tuple[float, float]) -> tuple[list[dict[str, object]], str]:
    root.mkdir()
    tasks = (
        _task("a", index=0, resources=ResourceClaims(writes=("a.txt",))),
        _task("b", index=1, resources=ResourceClaims(writes=("b.txt",))),
    )

    def make_handler(path: str, delay: float) -> object:
        async def handler(_request: TaskRequest, context: TaskContext) -> TaskOutcome:
            await asyncio.sleep(delay)
            (context.workspace_root / path).write_text(path, encoding="utf-8")
            return TaskOutcome.succeeded({"path": path})

        return handler

    scheduler, store, ledger = _scheduler(
        root,
        {
            tasks[0].capability_id: make_handler("a.txt", delays[0]),
            tasks[1].capability_id: make_handler("b.txt", delays[1]),
        },
    )
    asyncio.run(scheduler.run_wave(tuple(reversed(tasks))))
    events = [item.event.model_dump(mode="json") for item in ledger.read_all()]
    return events, store.head_tree_id()


def test_reverse_handler_completion_has_identical_events_and_tree(tmp_path: Path) -> None:
    first_events, first_tree = _run_duration_scenario(tmp_path / "first", (0.001, 0.02))
    second_events, second_tree = _run_duration_scenario(tmp_path / "second", (0.02, 0.001))

    assert first_events == second_events
    assert first_tree == second_tree
    assert canonical_digest(cast(JSONValue, first_events)) == canonical_digest(cast(JSONValue, second_events))


def test_deterministic_rebase_applies_add_change_and_delete(tmp_path: Path) -> None:
    tasks = (
        _task("left", index=0, resources=ResourceClaims(writes=("left",))),
        _task("right", index=1, resources=ResourceClaims(writes=("right",))),
    )

    async def edit(_request: TaskRequest, context: TaskContext) -> TaskOutcome:
        prefix = _request.node_id
        (context.workspace_root / prefix / "add.txt").write_bytes(b"added")
        (context.workspace_root / prefix / "change.txt").write_bytes(b"changed")
        (context.workspace_root / prefix / "delete.txt").unlink()
        return TaskOutcome.succeeded()

    initial = {
        f"{prefix}/{name}.txt": value
        for prefix in ("left", "right")
        for name, value in (("change", b"old"), ("delete", b"gone"))
    }
    scheduler, store, _ledger = _scheduler(
        tmp_path,
        {task.capability_id: edit for task in tasks},
        initial=initial,
    )

    results = asyncio.run(scheduler.run_wave(tasks))

    assert all(result.outcome.status == "succeeded" for result in results)
    for prefix in ("left", "right"):
        assert store.read_head(f"{prefix}/add.txt") == b"added"
        assert store.read_head(f"{prefix}/change.txt") == b"changed"
        with pytest.raises(WorkspaceViolation):
            store.read_head(f"{prefix}/delete.txt")


class _Validator:
    def __init__(self, result: ValidationResult | BaseException) -> None:
        self.result = result

    def validate(self, _candidate: CandidateWriteSet, _context: ValidationContext) -> ValidationResult:
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


@pytest.mark.parametrize(
    "validator",
    [_Validator(ValidationResult(accepted=False, reason="no")), _Validator(RuntimeError("boom"))],
)
def test_validator_rejection_or_exception_never_moves_head_or_emits_success(
    tmp_path: Path, validator: _Validator
) -> None:
    async def handler(_request: TaskRequest, context: TaskContext) -> TaskOutcome:
        (context.workspace_root / "out.txt").write_bytes(b"new")
        return TaskOutcome.succeeded()

    task = _task(
        "validated",
        resources=ResourceClaims(writes=("out.txt",)),
        validators=("test.validators.check",),
    )
    scheduler, store, ledger = _scheduler(
        tmp_path,
        {task.capability_id: handler},
        validators={"test.validators.check": validator},
    )
    before = store.head_tree_id()

    result = asyncio.run(scheduler.run_wave((task,)))[0]

    assert result.outcome.failure is not None
    assert result.outcome.failure.kind == "invalid_output"
    assert store.head_tree_id() == before
    assert all(
        item.event.kind not in {"task_attempt_succeeded", "head_advanced"} for item in ledger.read_all()
    )


def test_undeclared_candidate_and_commit_exception_fail_closed(tmp_path: Path) -> None:
    async def handler(_request: TaskRequest, context: TaskContext) -> TaskOutcome:
        (context.workspace_root / "undeclared.txt").write_bytes(b"new")
        return TaskOutcome.succeeded()

    task = _task("bad-write", resources=ResourceClaims(writes=("allowed",)))
    scheduler, store, ledger = _scheduler(tmp_path, {task.capability_id: handler})
    before = store.head_tree_id()

    result = asyncio.run(scheduler.run_wave((task,)))[0]

    assert result.outcome.failure is not None
    assert result.outcome.failure.kind == "internal"
    assert store.head_tree_id() == before
    assert all(item.event.kind != "task_attempt_succeeded" for item in ledger.read_all())


def _persist_lease(ledger: Ledger, lease: Lease) -> None:
    ledger.append_batch(
        (
            TaskAttemptStarted(
                activation_id=lease.activation_id,
                attempt=lease.attempt,
                lease_expires_at=str(lease.expires_at),
            ),
            TaskLeaseAcquired(**lease.model_dump()),
        ),
        expected_next_seq=1,
    )


def test_reclaim_uses_persisted_heartbeat_and_strict_expiry_boundary(tmp_path: Path) -> None:
    store = SnapshotStore.create(tmp_path / "store", {})
    ledger = Ledger(tmp_path / "ledger")
    lease = Lease(
        task_id="task-1",
        activation_id="activation-1",
        attempt=1,
        owner_id="old-worker",
        acquired_at=1.0,
        heartbeat_at=1.0,
        expires_at=11.0,
    )
    _persist_lease(ledger, lease)
    old = Scheduler(
        CapabilityRegistry.empty(),
        store,
        ledger,
        owner_id="old-worker",
        clock=FakeClock(5.0),
        lease_seconds=10.0,
    )
    persisted = old.heartbeat(lease)
    assert persisted.expires_at == 15.0

    at_boundary = Scheduler(
        CapabilityRegistry.empty(),
        store,
        Ledger(ledger.root),
        owner_id="new-worker",
        clock=FakeClock(15.0),
    )
    assert at_boundary.reclaim_expired((lease,)) == ()

    after_boundary = Scheduler(
        CapabilityRegistry.empty(),
        store,
        Ledger(ledger.root),
        owner_id="new-worker",
        clock=FakeClock(15.001),
    )
    assert after_boundary.reclaim_expired((lease,)) == ("task-1",)
    assert isinstance(ledger.read_all()[-1].event, TaskAttemptFailed)
    assert after_boundary.reclaim_expired((lease,)) == ()


def test_expired_lease_cannot_be_resurrected_by_heartbeat(tmp_path: Path) -> None:
    store = SnapshotStore.create(tmp_path / "store", {})
    ledger = Ledger(tmp_path / "ledger")
    lease = Lease(
        task_id="task-1",
        activation_id="activation-1",
        attempt=1,
        owner_id="worker-1",
        acquired_at=1.0,
        heartbeat_at=1.0,
        expires_at=11.0,
    )
    _persist_lease(ledger, lease)
    scheduler = Scheduler(
        CapabilityRegistry.empty(),
        store,
        ledger,
        owner_id="worker-1",
        clock=FakeClock(11.001),
    )

    with pytest.raises(ValueError, match="expired lease"):
        scheduler.heartbeat(lease)

    assert [item.event.kind for item in ledger.read_all()] == [
        "task_attempt_started",
        "task_lease_acquired",
    ]


def test_heartbeat_compare_and_append_rejects_concurrent_reclaim(tmp_path: Path) -> None:
    store = SnapshotStore.create(tmp_path / "store", {})
    ledger = Ledger(tmp_path / "ledger")
    lease = Lease(
        task_id="task-1",
        activation_id="activation-1",
        attempt=1,
        owner_id="worker-1",
        acquired_at=1.0,
        heartbeat_at=1.0,
        expires_at=11.0,
    )
    _persist_lease(ledger, lease)
    scheduler = Scheduler(
        CapabilityRegistry.empty(),
        store,
        ledger,
        owner_id="worker-1",
        clock=FakeClock(5.0),
    )
    original = ledger.append_batch

    def reclaim_first(events: object, expected_next_seq: int) -> object:
        Ledger(ledger.root).append_batch(
            (
                TaskAttemptFailed(
                    activation_id=lease.activation_id,
                    attempt=lease.attempt,
                    failure=TaskOutcome.failed("transient", "reclaimed").failure,  # type: ignore[arg-type]
                ),
            ),
            expected_next_seq=expected_next_seq,
        )
        return original(events, expected_next_seq)  # type: ignore[arg-type]

    ledger.append_batch = reclaim_first  # type: ignore[method-assign]

    with pytest.raises(LedgerConflictError):
        scheduler.heartbeat(lease)

    assert [item.event.kind for item in ledger.read_all()][-1] == "task_attempt_failed"
    assert all(item.event.kind != "task_lease_heartbeat" for item in ledger.read_all())


def test_reclaimed_handler_result_cannot_move_head_or_emit_success(tmp_path: Path) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    clock = FakeClock(100.0)

    async def handler(_request: TaskRequest, context: TaskContext) -> TaskOutcome:
        started.set()
        await release.wait()
        (context.workspace_root / "out.txt").write_bytes(b"late")
        return TaskOutcome.succeeded()

    task = _task("late", resources=ResourceClaims(writes=("out.txt",)))
    scheduler, store, ledger = _scheduler(
        tmp_path,
        {task.capability_id: handler},
        clock=clock,
    )
    recovery = Scheduler(
        CapabilityRegistry.empty(),
        store,
        Ledger(ledger.root),
        owner_id="recovery-worker",
        clock=clock,
    )
    before = store.head_tree_id()

    async def scenario() -> object:
        running = asyncio.create_task(scheduler.run_wave((task,)))
        await started.wait()
        clock.set(110.001)
        assert recovery.reclaim_expired() == (task.task_id,)
        release.set()
        return await running

    result = asyncio.run(scenario())[0]  # type: ignore[index]

    assert result.outcome.failure is not None
    assert result.outcome.failure.kind == "transient"
    assert store.head_tree_id() == before
    kinds = [item.event.kind for item in ledger.read_all()]
    assert kinds.count("task_attempt_failed") == 1
    assert "task_attempt_succeeded" not in kinds
    assert "head_advanced" not in kinds


def test_reclaim_between_commit_and_success_batch_rolls_head_back(tmp_path: Path) -> None:
    clock = FakeClock(100.0)

    async def handler(_request: TaskRequest, context: TaskContext) -> TaskOutcome:
        (context.workspace_root / "out.txt").write_bytes(b"candidate")
        return TaskOutcome.succeeded()

    task = _task("commit-race", resources=ResourceClaims(writes=("out.txt",)))
    scheduler, store, ledger = _scheduler(
        tmp_path,
        {task.capability_id: handler},
        clock=clock,
    )
    recovery = Scheduler(
        CapabilityRegistry.empty(),
        store,
        Ledger(ledger.root),
        owner_id="recovery-worker",
        clock=clock,
    )
    original_commit = store.commit_candidate

    def commit_then_reclaim(*args: object, **kwargs: object) -> object:
        result = original_commit(*args, **kwargs)  # type: ignore[arg-type]
        clock.set(110.001)
        assert recovery.reclaim_expired() == (task.task_id,)
        return result

    store.commit_candidate = commit_then_reclaim  # type: ignore[method-assign]
    before = store.head_tree_id()

    with pytest.raises(LedgerConflictError):
        asyncio.run(scheduler.run_wave((task,)))

    assert store.head_tree_id() == before
    kinds = [item.event.kind for item in ledger.read_all()]
    assert kinds.count("task_attempt_failed") == 1
    assert "task_attempt_succeeded" not in kinds
    assert "head_advanced" not in kinds


def test_unpersisted_or_tampered_lease_is_not_reclaimable(tmp_path: Path) -> None:
    scheduler, _store, _ledger = _scheduler(
        tmp_path,
        {},
        clock=FakeClock(100.0),
    )
    supplied = Lease(
        task_id="task-1",
        activation_id="activation-1",
        attempt=1,
        owner_id="worker",
        acquired_at=1.0,
        heartbeat_at=1.0,
        expires_at=2.0,
    )
    assert scheduler.reclaim_expired((supplied,)) == ()


def test_fold_persists_lease_heartbeat_and_head_transition() -> None:
    events = (
        InvocationStarted(invocation_id="inv-1", product_digest="a" * 64, entrypoint="main"),
        NodeActivated(
            activation_id="activation-1",
            graph_instance_id="graph-1",
            node_id="node-1",
            token_ids=(),
        ),
        TaskAttemptStarted(activation_id="activation-1", attempt=1, lease_expires_at="11"),
        TaskLeaseAcquired(
            task_id="task-1",
            activation_id="activation-1",
            attempt=1,
            owner_id="worker-1",
            acquired_at=1.0,
            heartbeat_at=1.0,
            expires_at=11.0,
        ),
        TaskLeaseHeartbeat(
            task_id="task-1",
            activation_id="activation-1",
            attempt=1,
            owner_id="worker-1",
            heartbeat_at=5.0,
            expires_at=15.0,
        ),
        TaskAttemptSucceeded(activation_id="activation-1", attempt=1, output={"ok": True}),
        HeadAdvanced(
            task_id="task-1",
            activation_id="activation-1",
            attempt=1,
            previous_tree_id="b" * 64,
            tree_id="c" * 64,
        ),
    )

    projection = fold_events(
        tuple(EventEnvelope.from_event(index, event) for index, event in enumerate(events, start=1))
    )

    attempt = projection.activations[0].attempts[0]
    assert attempt.lease_task_id == "task-1"
    assert attempt.lease_heartbeat_at == 5.0
    assert attempt.lease_expires_at_value == 15.0
    assert projection.head_tree_id == "c" * 64


def test_fold_rejects_head_advance_after_graph_failure() -> None:
    events = (
        InvocationStarted(invocation_id="inv-1", product_digest="a" * 64, entrypoint="main"),
        GraphStarted(graph_instance_id="graph-1", graph_id="graph-1"),
        NodeActivated(
            activation_id="activation-1",
            graph_instance_id="graph-1",
            node_id="node-1",
            token_ids=(),
        ),
        TaskAttemptStarted(activation_id="activation-1", attempt=1, lease_expires_at="11"),
        TaskLeaseAcquired(
            task_id="task-1",
            activation_id="activation-1",
            attempt=1,
            owner_id="worker-1",
            acquired_at=1.0,
            heartbeat_at=1.0,
            expires_at=11.0,
        ),
        TaskAttemptSucceeded(activation_id="activation-1", attempt=1, output=None),
        GraphFailed(graph_instance_id="graph-1", reason="failed"),
        HeadAdvanced(
            task_id="task-1",
            activation_id="activation-1",
            attempt=1,
            previous_tree_id="b" * 64,
            tree_id="c" * 64,
        ),
    )

    with pytest.raises(ProjectionError, match="already failed"):
        fold_events(
            tuple(EventEnvelope.from_event(index, event) for index, event in enumerate(events, start=1))
        )


def test_start_lease_and_success_head_are_atomic_ledger_batches(tmp_path: Path) -> None:
    async def handler(_request: TaskRequest, context: TaskContext) -> TaskOutcome:
        (context.workspace_root / "out.txt").write_bytes(b"ok")
        return TaskOutcome.succeeded()

    task = _task("atomic", resources=ResourceClaims(writes=("out.txt",)))
    scheduler, _store, ledger = _scheduler(tmp_path, {task.capability_id: handler})
    asyncio.run(scheduler.run_wave((task,)))

    batch_names = sorted(path.name for path in ledger.root.glob("*.json"))
    assert batch_names == ["0000000001-0000000002.json", "0000000003-0000000004.json"]
    assert [item.event.kind for item in ledger.read_all()] == [
        "task_attempt_started",
        "task_lease_acquired",
        "task_attempt_succeeded",
        "head_advanced",
    ]


def test_success_ledger_failure_rolls_head_back_without_success_event(tmp_path: Path) -> None:
    ledger_ref: list[Ledger] = []

    async def handler(_request: TaskRequest, context: TaskContext) -> TaskOutcome:
        (context.workspace_root / "out.txt").write_bytes(b"ok")
        ledger = ledger_ref[0]
        original = ledger.append_batch

        def fail_success(events: object, expected_next_seq: int) -> object:
            if any(getattr(event, "kind", None) == "task_attempt_succeeded" for event in events):  # type: ignore[union-attr]
                raise RuntimeError("ledger unavailable")
            return original(events, expected_next_seq)  # type: ignore[arg-type]

        ledger.append_batch = fail_success  # type: ignore[method-assign]
        return TaskOutcome.succeeded()

    task = _task("rollback", resources=ResourceClaims(writes=("out.txt",)))
    scheduler, store, ledger = _scheduler(tmp_path, {task.capability_id: handler})
    ledger_ref.append(ledger)
    before = store.head_tree_id()

    with pytest.raises(RuntimeError, match="ledger unavailable"):
        asyncio.run(scheduler.run_wave((task,)))

    assert store.head_tree_id() == before
    assert all(item.event.kind != "task_attempt_succeeded" for item in ledger.read_all())


def test_handler_receives_only_exact_attempt_workspace_capability(tmp_path: Path) -> None:
    seen: list[TaskContext] = []

    async def handler(_request: TaskRequest, context: TaskContext) -> TaskOutcome:
        seen.append(context)
        assert set(context.__dataclass_fields__) == {"workspace_root", "heartbeat"}
        assert context.workspace_root.name.endswith(".run")
        assert context.workspace_root.parent.name == "attempts"
        return TaskOutcome.succeeded()

    task = _task("bounded")
    scheduler, store, _ledger = _scheduler(tmp_path, {task.capability_id: handler})
    asyncio.run(scheduler.run_wave((task,)))

    assert seen[0].workspace_root != store.root
    assert seen[0].workspace_root != store.root / "trees"
