from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest
import graph_engine.runtime.ledger as ledger_runtime
import graph_engine.runtime.workspace as workspace_runtime

from graph_engine.canonical import JSONValue, canonical_digest, canonical_json_bytes
from graph_engine.plugin_api import (
    CandidateWriteSet,
    CapabilityRegistry,
    ResourceClaims,
    TaskContext,
    TaskHandler,
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
from graph_engine.runtime.scheduler import (
    FakeClock,
    LedgerPublicationIndeterminate,
    Lease,
    LeaseUnavailableError,
    Scheduler,
    SchedulerStateError,
    select_wave,
)
from graph_engine.runtime.workspace import (
    FinalizationRolledBack,
    HeadPublicationIndeterminate,
    SnapshotStore,
    WorkspaceViolation,
)


Handler = Callable[[TaskRequest, TaskContext], object]


class _InProcessTestHost:
    """Deliberately unconfined test double; never a production host."""

    def __init__(self) -> None:
        self.workspace_roots: list[Path] = []

    async def execute(
        self,
        handler: TaskHandler,
        request: TaskRequest,
        *,
        workspace_root: Path,
        heartbeat: Callable[[], None],
    ) -> TaskOutcome:
        self.workspace_roots.append(workspace_root)
        return await handler(
            request,
            TaskContext(workspace_root=workspace_root, heartbeat=heartbeat),
        )


def _task(
    name: str,
    *,
    rank: int = 0,
    index: int = 0,
    resources: ResourceClaims | None = None,
    timeout: float = 1.0,
    validators: tuple[str, ...] = (),
) -> PlannedTask:
    activation = f"activation-{name}"
    return PlannedTask(
        invocation_id="inv-1",
        task_id=canonical_digest({"activation_id": activation, "kind": "task"}),
        activation_id=activation,
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
    host: _InProcessTestHost | None = None,
) -> tuple[Scheduler, SnapshotStore, Ledger]:
    store = SnapshotStore.create(tmp_path / "store", initial or {})
    ledger = Ledger(tmp_path / "ledger")
    lifecycle: list[object] = [
        InvocationStarted(invocation_id="inv-1", product_digest="a" * 64, entrypoint="main"),
        GraphStarted(graph_instance_id="graph-1", graph_id="graph-1"),
    ]
    lifecycle.extend(
        NodeActivated(
            activation_id=f"activation-{capability_id.rsplit('.', 1)[-1]}",
            graph_instance_id="graph-1",
            node_id=capability_id.rsplit(".", 1)[-1],
            token_ids=(),
        )
        for capability_id in handlers
    )
    if handlers:
        ledger.append_batch(lifecycle, expected_next_seq=1)  # type: ignore[arg-type]
    scheduler = Scheduler(
        CapabilityRegistry(
            task_handlers=handlers,  # type: ignore[arg-type]
            commit_validators=validators or {},  # type: ignore[arg-type]
        ),
        store,
        ledger,
        host or _InProcessTestHost(),
        owner_id="worker-1",
        clock=clock or FakeClock(100.0),
        lease_seconds=10.0,
        max_parallel=max_parallel,
    )
    return scheduler, store, ledger


def _install_head_document(store: SnapshotStore, tree_id: str) -> None:
    tree_stat = (store.root / "trees" / tree_id).stat()
    payload: dict[str, object] = {
        "tree_id": tree_id,
        "tree_dev": tree_stat.st_dev,
        "tree_ino": tree_stat.st_ino,
    }
    document = {**payload, "digest": canonical_digest(cast(JSONValue, payload))}
    (store.root / "HEAD.json").write_bytes(canonical_json_bytes(cast(JSONValue, document)))


def _fail_ledger_boundary_once(
    monkeypatch: pytest.MonkeyPatch,
    failed_boundary: str,
    *,
    occurrence: int = 1,
) -> None:
    seen = 0

    def fail_once(name: str) -> None:
        nonlocal seen
        if name != failed_boundary:
            return
        seen += 1
        if seen == occurrence:
            raise OSError("append result unavailable")

    monkeypatch.setattr(ledger_runtime, "_append_boundary", fail_once)


def test_wave_uses_stable_topology_order_and_segment_aware_conflicts() -> None:
    first = _task("first", rank=0, index=1, resources=ResourceClaims(writes=("a/b",)))
    second = _task("second", rank=1, index=0, resources=ResourceClaims(reads=("a/bb",)))
    conflict = _task("conflict", rank=2, index=0, resources=ResourceClaims(reads=("a/b/c",)))

    selected = select_wave((conflict, second, first), max_parallel=4)

    assert [task.task_id for task in selected] == [first.task_id, second.task_id]


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
    assert [result.task.task_id for result in results] == [tasks[0].task_id, tasks[1].task_id]
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
    assert ledger.read_all()[-1].event.kind == "task_attempt_failed"
    projection = fold_events(ledger.read_all())
    assert projection.activations[0].attempts[-1].status == "failed"


def test_lease_expiry_during_validator_cannot_publish_success(tmp_path: Path) -> None:
    clock = FakeClock(100.0)

    async def handler(_request: TaskRequest, context: TaskContext) -> TaskOutcome:
        (context.workspace_root / "out.txt").write_bytes(b"candidate")
        return TaskOutcome.succeeded()

    class ExpiringValidator:
        def validate(self, _candidate: CandidateWriteSet, _context: ValidationContext) -> ValidationResult:
            clock.set(110.001)
            return ValidationResult(accepted=True)

    task = _task(
        "expires-in-validator",
        resources=ResourceClaims(writes=("out.txt",)),
        validators=("test.validators.expire",),
    )
    scheduler, store, ledger = _scheduler(
        tmp_path,
        {task.capability_id: handler},
        validators={"test.validators.expire": ExpiringValidator()},
        clock=clock,
    )
    before = store.head_tree_id()

    result = asyncio.run(scheduler.run_wave((task,)))[0]

    assert result.outcome.failure is not None
    assert result.outcome.failure.kind == "transient"
    assert store.head_tree_id() == before
    assert all(
        item.event.kind not in {"task_attempt_succeeded", "head_advanced"} for item in ledger.read_all()
    )
    assert ledger.read_all()[-1].event.kind == "task_attempt_failed"
    projection = fold_events(ledger.read_all())
    assert projection.activations[0].attempts[-1].status == "failed"


def test_lease_expiry_at_publication_boundary_conditionally_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock(100.0)

    async def handler(_request: TaskRequest, context: TaskContext) -> TaskOutcome:
        (context.workspace_root / "out.txt").write_bytes(b"candidate")
        return TaskOutcome.succeeded()

    task = _task("expires-at-publication", resources=ResourceClaims(writes=("out.txt",)))
    scheduler, store, ledger = _scheduler(
        tmp_path,
        {task.capability_id: handler},
        clock=clock,
    )
    before = store.head_tree_id()
    monkeypatch.setattr(
        workspace_runtime,
        "_finalization_boundary",
        lambda _name: clock.set(110.001),
    )

    with pytest.raises(FinalizationRolledBack) as raised:
        asyncio.run(scheduler.run_wave((task,)))

    assert isinstance(raised.value.__cause__, LeaseUnavailableError)
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
    existing = ledger.read_all()
    if not existing:
        ledger.append_batch(
            (
                InvocationStarted(invocation_id="inv-1", product_digest="a" * 64, entrypoint="main"),
                GraphStarted(graph_instance_id="graph-1", graph_id="graph-1"),
                NodeActivated(
                    activation_id=lease.activation_id,
                    graph_instance_id="graph-1",
                    node_id=lease.task_id,
                    token_ids=(),
                ),
            ),
            expected_next_seq=1,
        )
    else:
        projection = fold_events(existing)
        if all(item.activation_id != lease.activation_id for item in projection.activations):
            ledger.append_batch(
                (
                    NodeActivated(
                        activation_id=lease.activation_id,
                        graph_instance_id="graph-1",
                        node_id=lease.task_id,
                        token_ids=(),
                    ),
                ),
                expected_next_seq=existing[-1].seq + 1,
            )
    expected_next_seq = ledger.read_all()[-1].seq + 1
    ledger.append_batch(
        (
            TaskAttemptStarted(
                activation_id=lease.activation_id,
                attempt=lease.attempt,
                lease_expires_at=str(lease.expires_at),
            ),
            TaskLeaseAcquired(**lease.model_dump()),
        ),
        expected_next_seq=expected_next_seq,
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
        _InProcessTestHost(),
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
        _InProcessTestHost(),
        owner_id="new-worker",
        clock=FakeClock(15.0),
    )
    assert at_boundary.reclaim_expired((lease,)) == ()

    after_boundary = Scheduler(
        CapabilityRegistry.empty(),
        store,
        Ledger(ledger.root),
        _InProcessTestHost(),
        owner_id="new-worker",
        clock=FakeClock(15.001),
    )
    assert after_boundary.reclaim_expired((lease,)) == ("task-1",)
    assert isinstance(ledger.read_all()[-1].event, TaskAttemptFailed)
    assert after_boundary.reclaim_expired((lease,)) == ()


@pytest.mark.parametrize("failed_boundary", ["final_installed", "directory_fsynced"])
def test_authoritative_reclaim_append_is_reconciled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failed_boundary: str,
) -> None:
    store = SnapshotStore.create(tmp_path / "store", {})
    ledger = Ledger(tmp_path / "ledger")
    lease = Lease(
        task_id="task-1",
        activation_id="activation-1",
        attempt=1,
        owner_id="worker-1",
        acquired_at=1.0,
        heartbeat_at=1.0,
        expires_at=2.0,
    )
    _persist_lease(ledger, lease)
    scheduler = Scheduler(
        CapabilityRegistry.empty(),
        store,
        ledger,
        _InProcessTestHost(),
        owner_id="recovery-worker",
        clock=FakeClock(3.0),
    )
    _fail_ledger_boundary_once(monkeypatch, failed_boundary)

    assert scheduler.reclaim_expired() == (lease.task_id,)
    assert [item.event.kind for item in ledger.read_all()].count("task_attempt_failed") == 1


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
        _InProcessTestHost(),
        owner_id="worker-1",
        clock=FakeClock(11.001),
    )

    with pytest.raises(ValueError, match="expired lease"):
        scheduler.heartbeat(lease)

    assert [item.event.kind for item in ledger.read_all()][-2:] == [
        "task_attempt_started",
        "task_lease_acquired",
    ]


@pytest.mark.parametrize("failed_boundary", ["final_installed", "directory_fsynced"])
def test_authoritative_heartbeat_append_is_reconciled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failed_boundary: str,
) -> None:
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
        _InProcessTestHost(),
        owner_id="worker-1",
        clock=FakeClock(5.0),
    )
    _fail_ledger_boundary_once(monkeypatch, failed_boundary)

    updated = scheduler.heartbeat(lease)

    assert updated.heartbeat_at == 5.0
    assert [item.event.kind for item in ledger.read_all()].count("task_lease_heartbeat") == 1


def test_unreadable_generic_append_outcome_is_explicitly_indeterminate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
        _InProcessTestHost(),
        owner_id="worker-1",
        clock=FakeClock(5.0),
    )
    append_failed = False

    def fail_after_final_install(name: str) -> None:
        nonlocal append_failed
        if name == "final_installed":
            append_failed = True
            raise OSError("append result unavailable")

    original_read = ledger.read_all

    def fail_reconciliation_read() -> tuple[EventEnvelope, ...]:
        if append_failed:
            raise OSError("ledger unreadable")
        return original_read()

    monkeypatch.setattr(ledger_runtime, "_append_boundary", fail_after_final_install)
    ledger.read_all = fail_reconciliation_read  # type: ignore[method-assign]

    with pytest.raises(LedgerPublicationIndeterminate):
        scheduler.heartbeat(lease)

    kinds = [item.event.kind for item in Ledger(ledger.root).read_all()]
    assert kinds.count("task_lease_heartbeat") == 1


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
        _InProcessTestHost(),
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
        _InProcessTestHost(),
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


def test_reclaim_between_commit_and_success_batch_rolls_head_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
        _InProcessTestHost(),
        owner_id="recovery-worker",
        clock=clock,
    )

    def reclaim_after_publish(_name: str) -> None:
        clock.set(110.001)
        assert recovery.reclaim_expired() == (task.task_id,)

    monkeypatch.setattr(workspace_runtime, "_finalization_boundary", reclaim_after_publish)
    before = store.head_tree_id()

    with pytest.raises(FinalizationRolledBack):
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


@pytest.mark.parametrize("failed_boundary", ["final_installed", "directory_fsynced"])
@pytest.mark.parametrize("category", ["start", "failed", "stopped"])
def test_authoritative_start_and_terminal_appends_are_reconciled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failed_boundary: str,
    category: str,
) -> None:
    async def handler(_request: TaskRequest, _context: TaskContext) -> TaskOutcome:
        if category == "failed":
            return TaskOutcome.failed("internal", "closed failure")
        if category == "stopped":
            return TaskOutcome.stopped("closed stop")
        return TaskOutcome.succeeded()

    task = _task(f"reconcile-{category}")
    scheduler, _store, ledger = _scheduler(tmp_path, {task.capability_id: handler})
    _fail_ledger_boundary_once(
        monkeypatch,
        failed_boundary,
        occurrence=1 if category == "start" else 2,
    )

    result = asyncio.run(scheduler.run_wave((task,)))[0]

    assert result.outcome.status == ("succeeded" if category == "start" else category)
    kinds = [item.event.kind for item in ledger.read_all()]
    assert kinds.count("task_attempt_started") == 1
    expected_terminal = {
        "start": "task_attempt_succeeded",
        "failed": "task_attempt_failed",
        "stopped": "task_attempt_stopped",
    }[category]
    assert kinds.count(expected_terminal) == 1
    fold_events(ledger.read_all())


def test_start_lease_and_success_head_are_atomic_ledger_batches(tmp_path: Path) -> None:
    async def handler(_request: TaskRequest, context: TaskContext) -> TaskOutcome:
        (context.workspace_root / "out.txt").write_bytes(b"ok")
        return TaskOutcome.succeeded()

    task = _task("atomic", resources=ResourceClaims(writes=("out.txt",)))
    scheduler, _store, ledger = _scheduler(tmp_path, {task.capability_id: handler})
    asyncio.run(scheduler.run_wave((task,)))

    batch_names = sorted(path.name for path in ledger.root.glob("*.json"))
    assert batch_names == [
        "0000000001-0000000003.json",
        "0000000004-0000000005.json",
        "0000000006-0000000007.json",
    ]
    assert [item.event.kind for item in ledger.read_all()][-4:] == [
        "task_attempt_started",
        "task_lease_acquired",
        "task_attempt_succeeded",
        "head_advanced",
    ]
    projection = fold_events(ledger.read_all())
    head_event = ledger.read_all()[-1].event
    assert isinstance(head_event, HeadAdvanced)
    assert projection.head_tree_id == head_event.tree_id


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

    with pytest.raises(FinalizationRolledBack) as raised:
        asyncio.run(scheduler.run_wave((task,)))

    assert isinstance(raised.value.__cause__, RuntimeError)
    assert store.head_tree_id() == before
    assert all(item.event.kind != "task_attempt_succeeded" for item in ledger.read_all())


@pytest.mark.parametrize("failed_boundary", ["final_installed", "directory_fsynced"])
def test_success_append_error_after_authoritative_publication_keeps_exact_ledger_and_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failed_boundary: str,
) -> None:
    armed = False

    async def handler(_request: TaskRequest, context: TaskContext) -> TaskOutcome:
        nonlocal armed
        (context.workspace_root / "out.txt").write_bytes(b"ok")
        armed = True
        return TaskOutcome.succeeded()

    def fail_after_final_install(name: str) -> None:
        if armed and name == failed_boundary:
            raise OSError("append result unavailable")

    task = _task("installed-success", resources=ResourceClaims(writes=("out.txt",)))
    scheduler, store, ledger = _scheduler(tmp_path, {task.capability_id: handler})
    monkeypatch.setattr(ledger_runtime, "_append_boundary", fail_after_final_install)

    result = asyncio.run(scheduler.run_wave((task,)))[0]

    assert result.outcome.status == "succeeded"
    assert result.head_tree_id == store.head_tree_id()
    assert store.read_head("out.txt") == b"ok"
    kinds = [item.event.kind for item in ledger.read_all()]
    assert kinds.count("task_attempt_succeeded") == 1
    assert kinds.count("head_advanced") == 1


def test_unreadable_success_append_outcome_preserves_head_as_indeterminate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    armed = False
    append_failed = False

    async def handler(_request: TaskRequest, context: TaskContext) -> TaskOutcome:
        nonlocal armed
        (context.workspace_root / "out.txt").write_bytes(b"ok")
        armed = True
        return TaskOutcome.succeeded()

    def fail_after_final_install(name: str) -> None:
        nonlocal append_failed
        if armed and name == "final_installed":
            append_failed = True
            raise OSError("append result unavailable")

    task = _task("unreadable-success", resources=ResourceClaims(writes=("out.txt",)))
    scheduler, store, ledger = _scheduler(tmp_path, {task.capability_id: handler})
    original_read = ledger.read_all

    def fail_reconciliation_read() -> tuple[EventEnvelope, ...]:
        if append_failed:
            raise OSError("ledger unreadable")
        return original_read()

    monkeypatch.setattr(ledger_runtime, "_append_boundary", fail_after_final_install)
    ledger.read_all = fail_reconciliation_read  # type: ignore[method-assign]

    with pytest.raises(HeadPublicationIndeterminate) as raised:
        asyncio.run(scheduler.run_wave((task,)))

    assert isinstance(raised.value.__cause__, OSError)
    assert store.read_head("out.txt") == b"ok"
    kinds = [item.event.kind for item in Ledger(ledger.root).read_all()]
    assert kinds.count("task_attempt_succeeded") == 1
    assert kinds.count("head_advanced") == 1


def test_success_ledger_and_rollback_failure_is_explicitly_indeterminate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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

    def fail_restore(_self: object, _root_fd: int, _previous: bytes | None) -> None:
        raise OSError("rollback unavailable")

    task = _task("indeterminate-rollback", resources=ResourceClaims(writes=("out.txt",)))
    scheduler, store, ledger = _scheduler(tmp_path, {task.capability_id: handler})
    ledger_ref.append(ledger)
    monkeypatch.setattr(SnapshotStore, "_restore_previous_head", fail_restore)

    with pytest.raises(HeadPublicationIndeterminate) as raised:
        asyncio.run(scheduler.run_wave((task,)))

    assert isinstance(raised.value.__cause__, OSError)
    assert all(item.event.kind != "task_attempt_succeeded" for item in ledger.read_all())
    assert store.read_head("out.txt") == b"ok"


def test_losing_success_cas_never_overwrites_or_records_newer_head(tmp_path: Path) -> None:
    async def handler(_request: TaskRequest, context: TaskContext) -> TaskOutcome:
        (context.workspace_root / "a.txt").write_bytes(b"A")
        return TaskOutcome.succeeded()

    task = _task("writer-a", resources=ResourceClaims(writes=("a.txt",)))
    scheduler, store, ledger = _scheduler(tmp_path, {task.capability_id: handler})

    newer = store.create_attempt("newer-head")
    (newer.root / "a.txt").write_bytes(b"A")
    (newer.root / "b.txt").write_bytes(b"B")
    newer_candidate = newer.seal()
    newer.discard()

    lease_b = Lease(
        task_id="task-writer-b",
        activation_id="activation-writer-b",
        attempt=1,
        owner_id="worker-b",
        acquired_at=100.0,
        heartbeat_at=100.0,
        expires_at=1000.0,
    )
    _persist_lease(ledger, lease_b)
    original_append = ledger.append_batch

    def append_newer_success_first(events: object, expected_next_seq: int) -> object:
        materialized = tuple(events)  # type: ignore[arg-type]
        a_head = next(
            (event for event in materialized if isinstance(event, HeadAdvanced)),
            None,
        )
        if a_head is None:
            return original_append(materialized, expected_next_seq)
        _install_head_document(store, newer_candidate.candidate_tree_id)
        Ledger(ledger.root).append_batch(
            (
                TaskAttemptSucceeded(
                    activation_id=lease_b.activation_id,
                    attempt=lease_b.attempt,
                    output=None,
                ),
                HeadAdvanced(
                    task_id=lease_b.task_id,
                    activation_id=lease_b.activation_id,
                    attempt=lease_b.attempt,
                    previous_tree_id=a_head.tree_id,
                    tree_id=newer_candidate.candidate_tree_id,
                ),
            ),
            expected_next_seq=expected_next_seq,
        )
        return original_append(materialized, expected_next_seq)

    ledger.append_batch = append_newer_success_first  # type: ignore[method-assign]

    with pytest.raises(HeadPublicationIndeterminate):
        asyncio.run(scheduler.run_wave((task,)))

    assert store.head_tree_id() == newer_candidate.candidate_tree_id
    events = [envelope.event for envelope in ledger.read_all()]
    assert any(
        isinstance(event, TaskAttemptSucceeded) and event.activation_id == lease_b.activation_id
        for event in events
    )
    assert all(
        not isinstance(event, TaskAttemptSucceeded) or event.activation_id != task.activation_id
        for event in events
    )


def test_explicit_test_host_receives_the_exact_attempt_root(tmp_path: Path) -> None:
    seen: list[TaskContext] = []
    host = _InProcessTestHost()

    async def handler(_request: TaskRequest, context: TaskContext) -> TaskOutcome:
        seen.append(context)
        assert set(context.__dataclass_fields__) == {"workspace_root", "heartbeat"}
        assert context.workspace_root.name.endswith(".run")
        assert context.workspace_root.parent.name == "attempts"
        return TaskOutcome.succeeded()

    task = _task("bounded")
    scheduler, store, _ledger = _scheduler(
        tmp_path,
        {task.capability_id: handler},
        host=host,
    )
    asyncio.run(scheduler.run_wave((task,)))

    assert host.workspace_roots == [seen[0].workspace_root]
    assert seen[0].workspace_root != store.root
    assert seen[0].workspace_root != store.root / "trees"


def test_start_rejects_task_without_folded_active_activation(tmp_path: Path) -> None:
    async def handler(_request: TaskRequest, _context: TaskContext) -> TaskOutcome:
        return TaskOutcome.succeeded()

    task = _task("unplanned")
    store = SnapshotStore.create(tmp_path / "store", {})
    ledger = Ledger(tmp_path / "ledger")
    scheduler = Scheduler(
        CapabilityRegistry(
            task_handlers={task.capability_id: handler},  # type: ignore[arg-type]
            commit_validators={},
        ),
        store,
        ledger,
        _InProcessTestHost(),
        owner_id="worker-1",
        clock=FakeClock(100.0),
    )

    with pytest.raises(SchedulerStateError, match="invocation is not running"):
        asyncio.run(scheduler.run_wave((task,)))

    assert ledger.read_all() == ()


def test_start_rejects_task_whose_folded_graph_is_closed(tmp_path: Path) -> None:
    async def handler(_request: TaskRequest, _context: TaskContext) -> TaskOutcome:
        return TaskOutcome.succeeded()

    task = _task("closed-graph")
    scheduler, _store, ledger = _scheduler(tmp_path, {task.capability_id: handler})
    envelopes = ledger.read_all()
    ledger.append_batch(
        (GraphFailed(graph_instance_id=task.graph_instance_id, reason="closed"),),
        expected_next_seq=envelopes[-1].seq + 1,
    )

    with pytest.raises(SchedulerStateError, match="graph is not running"):
        asyncio.run(scheduler.run_wave((task,)))

    assert all(item.event.kind != "task_attempt_started" for item in ledger.read_all())


def test_start_rejects_task_id_not_derived_from_folded_activation(tmp_path: Path) -> None:
    async def handler(_request: TaskRequest, _context: TaskContext) -> TaskOutcome:
        return TaskOutcome.succeeded()

    task = _task("tampered-task-id")
    scheduler, _store, ledger = _scheduler(tmp_path, {task.capability_id: handler})
    tampered = task.model_copy(update={"task_id": "not-the-planned-task"})

    with pytest.raises(SchedulerStateError, match="task id does not match activation"):
        asyncio.run(scheduler.run_wave((tampered,)))

    assert all(item.event.kind != "task_attempt_started" for item in ledger.read_all())


def test_sequential_duplicate_attempt_start_is_semantically_rejected(tmp_path: Path) -> None:
    async def handler(_request: TaskRequest, _context: TaskContext) -> TaskOutcome:
        return TaskOutcome.succeeded()

    task = _task("duplicate")
    scheduler, store, ledger = _scheduler(tmp_path, {task.capability_id: handler})
    first = asyncio.run(scheduler.run_wave((task,)))
    head_after_first = store.head_tree_id()
    events_after_first = ledger.read_all()

    with pytest.raises(SchedulerStateError, match="expected task attempt 2"):
        asyncio.run(scheduler.run_wave((task,)))

    assert first[0].outcome.status == "succeeded"
    assert store.head_tree_id() == head_after_first
    assert ledger.read_all() == events_after_first


def test_concurrent_duplicate_attempt_start_has_one_semantic_winner(tmp_path: Path) -> None:
    async def handler(_request: TaskRequest, _context: TaskContext) -> TaskOutcome:
        await asyncio.sleep(0)
        return TaskOutcome.succeeded()

    task = _task("duplicate-race")
    first, store, ledger = _scheduler(tmp_path, {task.capability_id: handler})
    second = Scheduler(
        CapabilityRegistry(
            task_handlers={task.capability_id: handler},  # type: ignore[arg-type]
            commit_validators={},
        ),
        store,
        Ledger(ledger.root),
        _InProcessTestHost(),
        owner_id="worker-2",
        clock=FakeClock(100.0),
        lease_seconds=10.0,
        max_parallel=1,
    )

    async def race() -> tuple[object, object]:
        results = await asyncio.gather(
            first.run_wave((task,)),
            second.run_wave((task,)),
            return_exceptions=True,
        )
        return results[0], results[1]

    outcomes = asyncio.run(race())

    assert sum(isinstance(outcome, SchedulerStateError) for outcome in outcomes) == 1
    assert sum(isinstance(outcome, tuple) for outcome in outcomes) == 1
    assert [item.event.kind for item in ledger.read_all()].count("task_attempt_started") == 1


def test_next_attempt_requires_prior_failed_outcome(tmp_path: Path) -> None:
    async def handler(_request: TaskRequest, _context: TaskContext) -> TaskOutcome:
        return TaskOutcome.succeeded()

    first_task = _task("retry-state")
    scheduler, _store, ledger = _scheduler(tmp_path, {first_task.capability_id: handler})
    asyncio.run(scheduler.run_wave((first_task,)))
    second_task = first_task.model_copy(update={"attempt": 2})
    events_after_first = ledger.read_all()

    with pytest.raises(SchedulerStateError, match="requires a failed prior attempt"):
        asyncio.run(scheduler.run_wave((second_task,)))

    assert ledger.read_all() == events_after_first


def test_failed_attempt_retry_must_match_folded_prior_failure(tmp_path: Path) -> None:
    async def handler(request: TaskRequest, _context: TaskContext) -> TaskOutcome:
        if request.attempt == 1:
            return TaskOutcome.failed("transient", "retry me")
        return TaskOutcome.succeeded()

    first_task = _task("valid-retry")
    scheduler, _store, ledger = _scheduler(tmp_path, {first_task.capability_id: handler})
    first = asyncio.run(scheduler.run_wave((first_task,)))[0]
    assert first.outcome.failure is not None
    second_task = first_task.model_copy(update={"attempt": 2, "prior_failure": first.outcome.failure})

    second = asyncio.run(scheduler.run_wave((second_task,)))[0]

    assert second.outcome.status == "succeeded"
    projection = fold_events(ledger.read_all())
    attempts = projection.activations[0].attempts
    assert [attempt.status for attempt in attempts] == ["failed", "succeeded"]


def test_wave_attempts_use_one_baseline_despite_commit_between_creations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed: dict[str, bytes] = {}
    tasks = (
        _task("first", index=0, resources=ResourceClaims(reads=("seed.txt",))),
        _task("second", index=1, resources=ResourceClaims(reads=("seed.txt",))),
    )

    async def handler(request: TaskRequest, context: TaskContext) -> TaskOutcome:
        observed[request.node_id] = (context.workspace_root / "seed.txt").read_bytes()
        return TaskOutcome.succeeded()

    scheduler, store, _ledger = _scheduler(
        tmp_path,
        {task.capability_id: handler for task in tasks},
        initial={"seed.txt": b"H0"},
    )
    external = store.create_attempt("external")
    (external.root / "seed.txt").write_bytes(b"H1")
    external_candidate = external.seal()
    external.discard()

    def external_commit_after_first_copy(created_count: int, _baseline_tree_id: str) -> None:
        if created_count == 1:
            _install_head_document(store, external_candidate.candidate_tree_id)

    monkeypatch.setattr(
        workspace_runtime,
        "_attempt_batch_boundary",
        external_commit_after_first_copy,
    )

    asyncio.run(scheduler.run_wave(tasks))

    assert observed == {"first": b"H0", "second": b"H0"}
