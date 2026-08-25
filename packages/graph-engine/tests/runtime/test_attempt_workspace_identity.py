from __future__ import annotations

from bootstrap_fixtures import synthetic_invocation_started
import ast
import asyncio
import hashlib
import inspect
import os
from pathlib import Path

import pytest

from graph_engine.canonical import canonical_digest
from graph_engine.plugin_api import (
    RecoverableTaskHandler,
    ResourceClaims,
    TaskActivityCancelResult,
    TaskActivityReconcileResult,
    TaskActivitySnapshot,
    TaskContext,
    TaskHandler,
    TaskOutcome,
    TaskRequest,
)
from graph_engine.runtime.events import (
    GraphStarted,
    NodeActivated,
)
from graph_engine.runtime.host_protocol import TaskHostCallResult, TaskHostExecuteCall
from graph_engine.runtime.ledger import Ledger
from graph_engine.runtime.models import (
    PlannedTask,
    activity_id_for_attempt,
    fold_events,
)
from graph_engine.runtime.scheduler import FakeClock, Scheduler
from graph_engine.runtime.task_workspace import TaskWorkspaceStore, TaskWorkspaceViolation


_LOCK_DIGEST = "a" * 64


class _RecoverableHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del request, context
        return TaskOutcome.succeeded()

    async def reconcile(
        self,
        request: TaskRequest,
        context: TaskContext,
        activity: TaskActivitySnapshot,
    ) -> TaskActivityReconcileResult:
        del request, context, activity
        return TaskActivityReconcileResult(status="not_dispatched")

    async def cancel(
        self,
        request: TaskRequest,
        context: TaskContext,
        activity: TaskActivitySnapshot,
    ) -> TaskActivityCancelResult:
        del request, context, activity
        return TaskActivityCancelResult(status="acknowledged")


class _DirectRegistry:
    def __init__(self, handlers: dict[str, TaskHandler]) -> None:
        self.task_handlers = handlers
        self.commit_validators: dict[str, object] = {}
        self.bindings: dict[str, object] = {}


class _InProcessTestHost:
    def __init__(self) -> None:
        self._handlers: dict[str, TaskHandler] = {}
        self._store: TaskWorkspaceStore | None = None

    def bind_invocation_runtime(
        self,
        *,
        handlers: dict[str, TaskHandler],
        store: TaskWorkspaceStore,
    ) -> None:
        self._handlers = handlers
        self._store = store

    async def execute(self, call: object) -> object:
        del call
        raise AssertionError("host execute must stay unwired in workspace identity tests")

    async def reconcile(self, call: object) -> object:
        del call
        raise AssertionError("reconcile must stay unwired")

    async def cancel(self, call: object) -> object:
        del call
        raise AssertionError("cancel must stay unwired")

    def read_terminal_receipts(self, identity: object) -> tuple[()]:
        del identity
        return ()


class _ExecutingTestHost(_InProcessTestHost):
    async def execute(self, call: TaskHostExecuteCall) -> TaskHostCallResult:
        assert self._store is not None
        handler = self._handlers[call.request.capability_id]
        identity = call.attempt_root.workspace_identity
        binding = self._store.begin(
            task_id=identity.task_id,
            attempt=identity.attempt,
            output_paths=identity.output_paths,
        )
        assert binding.identity == identity
        outcome = await handler.execute(
            call.request,
            TaskContext(
                project_root=binding.project_root,
                write_root=binding.write_root,
                workspace_identity=binding.identity,
                heartbeat=lambda: None,
                cancel_requested=lambda: False,
                invocation=call.request.invocation,
            ),
        )
        return TaskHostCallResult(operation="execute", outcome=outcome)


def _task() -> PlannedTask:
    activation = "activation-work"
    return PlannedTask(
        invocation_id="inv-1",
        task_id=canonical_digest({"activation_id": activation, "kind": "task"}),
        activation_id=activation,
        graph_instance_id="graph-1",
        node_id="work",
        capability_id="test.tasks.work",
        attempt=1,
        input={"name": "work"},
        timeout_seconds=1.0,
        resources=ResourceClaims(),
        validators=(),
        topology_rank=0,
        declaration_index=0,
    )


def _scheduler_with_boundaries(
    tmp_path: Path,
    *,
    host: _InProcessTestHost | None = None,
) -> tuple[Scheduler, Ledger, TaskWorkspaceStore]:
    task = _task()
    handler = _RecoverableHandler()
    assert isinstance(handler, RecoverableTaskHandler)
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / "seed.txt").write_bytes(b"seed")
    store = TaskWorkspaceStore(
        project_root,
        tmp_path / "attempts",
        tmp_path / "promotion-receipts",
    )
    ledger = Ledger(tmp_path / "ledger")
    ledger.append_batch(
        (
            synthetic_invocation_started(lock_digest=_LOCK_DIGEST),
            GraphStarted(graph_instance_id="graph-1", graph_id="graph-1"),
            NodeActivated(
                activation_id=task.activation_id,
                graph_instance_id="graph-1",
                node_id=task.node_id,
                token_ids=(),
            ),
        ),
        expected_next_seq=1,
    )
    scheduler = Scheduler(
        _DirectRegistry({task.capability_id: handler}),
        store,
        ledger,
        host if host is not None else _InProcessTestHost(),
        owner_id="worker-1",
        clock=FakeClock(100.0),
        lease_seconds=10.0,
        lock_digest=_LOCK_DIGEST,
    )
    return scheduler, ledger, store


def _prepared_attempt_store(tmp_path: Path) -> tuple[TaskWorkspaceStore, object]:
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / "seed.txt").write_bytes(b"seed")
    store = TaskWorkspaceStore(
        project_root,
        tmp_path / "attempts",
        tmp_path / "promotion-receipts",
    )
    binding = store.begin(task_id="task-1", attempt=1, output_paths=("seed.txt",))
    return store, binding.identity


def _task_root(store: TaskWorkspaceStore, task_id: str) -> Path:
    return store.attempts_root / hashlib.sha256(task_id.encode("utf-8")).hexdigest()


def _mutate_attempt(store: TaskWorkspaceStore, identity: object, mutation: str) -> None:
    root = _task_root(store, identity.task_id) / identity.attempt_id  # type: ignore[attr-defined]
    if mutation == "missing":
        root.rmdir()
        return
    if mutation == "symlink":
        root.rmdir()
        outside = store.attempts_root.parent / "outside"
        outside.mkdir()
        (outside / "escaped.txt").write_bytes(b"escaped")
        root.symlink_to(outside, target_is_directory=True)
        return
    raise AssertionError(f"unknown mutation: {mutation}")


def test_recoverable_start_creates_workspace_before_atomic_initial_batch(tmp_path: Path) -> None:
    scheduler, ledger, store = _scheduler_with_boundaries(tmp_path)
    _lease, workspace, identity = scheduler.start_recoverable(_task())
    assert workspace.write_root == _task_root(store, identity.task_id) / identity.attempt_id
    assert workspace.write_root.is_dir()
    assert [envelope.event.kind for envelope in ledger.read_all()[-3:]] == [
        "task_attempt_started",
        "task_lease_acquired",
        "task_activity_prepared",
    ]


@pytest.mark.parametrize("mutation", ["missing", "symlink"])
def test_recovery_never_recreates_lost_prepared_workspace(tmp_path: Path, mutation: str) -> None:
    store, identity = _prepared_attempt_store(tmp_path)
    _mutate_attempt(store, identity, mutation)
    with pytest.raises((OSError, TaskWorkspaceViolation)):
        store.begin(
            task_id=identity.task_id,  # type: ignore[attr-defined]
            attempt=identity.attempt,  # type: ignore[attr-defined]
            output_paths=identity.output_paths,  # type: ignore[attr-defined]
        )


def test_identity_never_serializes_an_unrestricted_path(tmp_path: Path) -> None:
    store, identity = _prepared_attempt_store(tmp_path)
    dumped = identity.model_dump(mode="json")
    encoded = identity.model_dump_json()
    assert dumped["attempt_id"] == "attempt-1"
    assert "/" not in dumped["attempt_id"]
    assert "\\" not in dumped["attempt_id"]
    assert str(store.project_root) not in encoded
    assert str(store.attempts_root) not in encoded
    assert os.fsdecode(store.project_root) not in encoded
    for value in dumped.values():
        if isinstance(value, str):
            assert not value.startswith("/")
            assert "\\" not in value


def test_activity_recovery_paths_do_not_invoke_reset_or_create_attempts() -> None:
    import graph_engine.runtime.scheduler as scheduler_runtime
    import graph_engine.runtime.task_workspace as workspace_runtime

    forbidden = {"reset_attempt", "create_attempts"}
    scanned = {
        "Scheduler.start_recoverable": _calls_in_class_method(
            scheduler_runtime.Scheduler, "start_recoverable"
        ),
        "TaskWorkspaceStore.begin": _calls_in_class_method(workspace_runtime.TaskWorkspaceStore, "begin"),
        "TaskWorkspaceStore._binding_paths": _calls_in_class_method(
            workspace_runtime.TaskWorkspaceStore, "_binding_paths"
        ),
        "TaskWorkspaceStore._authenticate_identity": _calls_in_class_method(
            workspace_runtime.TaskWorkspaceStore, "_authenticate_identity"
        ),
    }
    for label, names in scanned.items():
        assert forbidden.isdisjoint(names), f"{label} invoked {sorted(forbidden & names)}"


def test_absent_initial_batch_removes_only_the_authenticated_orphan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scheduler, ledger, store = _scheduler_with_boundaries(tmp_path)
    task = _task()
    attempt_root = _task_root(store, task.task_id) / "attempt-1"
    sibling = store.begin(task_id="task-other", attempt=1, output_paths=())

    def fail_append(*_args: object, **_kwargs: object) -> object:
        raise OSError("simulated absent publication")

    monkeypatch.setattr(ledger, "append_batch", fail_append)
    with pytest.raises(OSError, match="simulated absent publication"):
        scheduler.start_recoverable(task)

    assert attempt_root.is_dir()
    reopened = store.begin(task_id=task.task_id, attempt=task.attempt, output_paths=())
    assert reopened.write_root == attempt_root
    assert sibling.write_root.exists()


def test_ambiguous_initial_batch_retains_the_attempt_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from graph_engine.runtime.ledger import LedgerPublicationIndeterminate

    scheduler, ledger, store = _scheduler_with_boundaries(tmp_path)
    task = _task()
    attempt_root = _task_root(store, task.task_id) / "attempt-1"

    def ambiguous_append(*_args: object, **_kwargs: object) -> object:
        raise LedgerPublicationIndeterminate("publication outcome is indeterminate")

    monkeypatch.setattr(type(ledger), "append_batch", ambiguous_append)
    with pytest.raises(LedgerPublicationIndeterminate):
        scheduler.start_recoverable(task)

    assert attempt_root.is_dir()


def test_recoverable_run_wave_does_not_promote_attempt_outcome_before_terminal(
    tmp_path: Path,
) -> None:
    scheduler, ledger, store = _scheduler_with_boundaries(tmp_path, host=_ExecutingTestHost())
    task = _task()
    results = asyncio.run(scheduler.run_wave((task,)))
    kinds = [envelope.event.kind for envelope in ledger.read_all()]
    assert "task_activity_terminal_observed" not in kinds
    assert "task_attempt_succeeded" not in kinds
    assert "task_attempt_failed" not in kinds
    assert "task_attempt_stopped" not in kinds
    assert "task_commit_prepared" not in kinds
    assert results[0].outcome.status == "succeeded"
    projection = fold_events(tuple(ledger.read_all()))
    activation = next(item for item in projection.activations if item.activation_id == task.activation_id)
    attempt = activation.attempts[-1]
    assert attempt.status == "running"
    assert attempt.activity is not None
    assert attempt.activity.state == "prepared"
    assert (_task_root(store, task.task_id) / "attempt-1").exists()


@pytest.mark.parametrize("orphan", ["unauthenticated", "authenticated"])
def test_start_reuses_only_an_authenticated_pre_batch_workspace(tmp_path: Path, orphan: str) -> None:
    scheduler, ledger, store = _scheduler_with_boundaries(tmp_path)
    task = _task()
    task_root = _task_root(store, task.task_id)
    attempt_root = task_root / "attempt-1"
    if orphan == "authenticated":
        expected = store.begin(task_id=task.task_id, attempt=task.attempt, output_paths=())
    else:
        task_root.mkdir()
        attempt_root.mkdir()
        expected = None
    assert attempt_root.exists()
    if expected is None:
        with pytest.raises(TaskWorkspaceViolation):
            scheduler.start_recoverable(task)
        assert len(ledger.read_all()) == 3
        return
    _lease, reopened, identity = scheduler.start_recoverable(task)
    assert [envelope.event.kind for envelope in ledger.read_all()[-3:]] == [
        "task_attempt_started",
        "task_lease_acquired",
        "task_activity_prepared",
    ]
    assert reopened.identity == expected.identity == identity
    assert reopened.write_root == attempt_root


def test_prepared_activity_uses_derived_ids(tmp_path: Path) -> None:
    scheduler, ledger, store = _scheduler_with_boundaries(tmp_path)
    task = _task()
    scheduler.start_recoverable(task)
    prepared = ledger.read_all()[-1].event
    assert prepared.kind == "task_activity_prepared"
    assert prepared.activity_id == activity_id_for_attempt(
        invocation_id=task.invocation_id,
        task_id=task.task_id,
        activation_id=task.activation_id,
        attempt=task.attempt,
    )
    assert prepared.workspace_identity.task_id == task.task_id
    assert prepared.workspace_identity.attempt_id == "attempt-1"
    reopened = store.begin(
        task_id=task.task_id,
        attempt=task.attempt,
        output_paths=task.resources.writes,
    )
    assert reopened.identity == prepared.workspace_identity
    assert reopened.project_root == store.project_root


def _calls_in_class_method(cls: type[object], method_name: str) -> set[str]:
    source = inspect.getsource(cls)
    tree = ast.parse(source)
    class_node = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == cls.__name__
    )
    method = next(
        node
        for node in class_node.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == method_name
    )
    names: set[str] = set()
    for node in ast.walk(method):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute):
                names.add(func.attr)
            elif isinstance(func, ast.Name):
                names.add(func.id)
    return names
