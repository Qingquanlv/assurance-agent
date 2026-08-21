from __future__ import annotations

import asyncio
import builtins
import importlib.util
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import pytest

from graph_engine.canonical import canonical_digest
from graph_engine.plugin_api import (
    RecoverableTaskHandler,
    TaskActivityCancelResult,
    TaskActivityReconcileResult,
    TaskActivitySnapshot,
    TaskContext,
    TaskHandler,
    TaskOutcome,
    TaskRequest,
)
from graph_engine.runtime.engine import Engine
from graph_engine.runtime.host_protocol import TaskHostCallResult
from graph_engine.runtime.ledger import Ledger
from graph_engine.runtime.models import fold_events
from graph_engine.runtime.planner import plan_next
from graph_engine.runtime.scheduler import FakeClock, Scheduler
from graph_engine.runtime.workspace import SnapshotStore


_ReconcileStatus = Literal["not_dispatched", "running", "terminal", "absent", "indeterminate"]
_DECISIONS = (
    ("not_dispatched", "execute_same_attempt"),
    ("running", "adopt_same_attempt"),
    ("terminal", "promote_same_attempt"),
    ("absent", "finalize_failure_then_retry_policy"),
    ("indeterminate", "block"),
)


class CallLog:
    def __init__(self) -> None:
        self.order: list[str] = []


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


class _RecordingHost:
    def __init__(
        self,
        *,
        status: _ReconcileStatus | None = None,
        reconcile_error: BaseException | None = None,
        malformed: bool = False,
        calls: CallLog | None = None,
    ) -> None:
        self._status = status
        self._reconcile_error = reconcile_error
        self._malformed = malformed
        self.calls = calls if calls is not None else CallLog()
        self._handlers: dict[str, TaskHandler] = {}
        self._store: SnapshotStore | None = None

    def bind_invocation_runtime(
        self,
        *,
        handlers: dict[str, TaskHandler],
        store: SnapshotStore,
    ) -> None:
        self._handlers = handlers
        self._store = store

    async def execute(self, call: object) -> TaskHostCallResult:
        del call
        raise AssertionError("execute must not run during recovery-order tests")

    async def reconcile(self, call: object) -> object:
        del call
        self.calls.order.append("reconcile")
        if self._reconcile_error is not None:
            raise self._reconcile_error
        if self._malformed:
            return TaskHostCallResult(operation="execute", outcome=TaskOutcome.succeeded({"ok": True}))
        assert self._status is not None
        return TaskHostCallResult(
            operation="reconcile",
            reconcile_result=_reconcile_result(self._status),
        )

    async def cancel(self, call: object) -> TaskHostCallResult:
        del call
        self.calls.order.append("cancel")
        return TaskHostCallResult(
            operation="cancel",
            cancel_result=TaskActivityCancelResult(status="acknowledged"),
        )

    def read_terminal_receipts(self, identity: object) -> tuple[()]:
        del identity
        return ()


@dataclass
class _RecoveryFixture:
    handle: Any
    engine: Engine
    product: Any
    calls: CallLog
    ledger: Ledger
    invocation_id: str
    attempt_before: int

    def observed_decision(self, result: Any) -> str:
        decisions = getattr(result, "decisions", ())
        assert len(decisions) == 1
        return decisions[0].decision


def _reconcile_result(status: _ReconcileStatus) -> TaskActivityReconcileResult:
    if status == "not_dispatched":
        return TaskActivityReconcileResult(status="not_dispatched")
    if status == "running":
        return TaskActivityReconcileResult(status="running")
    if status == "terminal":
        return TaskActivityReconcileResult(
            status="terminal",
            outcome=TaskOutcome.failed("transient", "provider finished"),
        )
    if status == "absent":
        return TaskActivityReconcileResult(status="absent", proof={"kind": "never_created"})
    return TaskActivityReconcileResult(status="indeterminate", reason="provider timeout")


def _load_engine_helpers() -> Any:
    names = (
        "_graph_engine_runtime_test_callbacks",
        "_graph_engine_runtime_test_effect_callbacks",
        "_graph_engine_runtime_test_effect_policies",
    )
    saved = {name: getattr(builtins, name, None) for name in names}
    path = Path(__file__).with_name("test_engine.py")
    spec = importlib.util.spec_from_file_location("_recovery_engine_helpers", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._restored_builtins = saved  # type: ignore[attr-defined]
    return module


def _task_product(handler: Any) -> Any:
    helpers = _load_engine_helpers()
    try:
        return helpers._task_product(handler)
    finally:
        for name, value in helpers._restored_builtins.items():
            if value is None:
                if hasattr(builtins, name):
                    delattr(builtins, name)
            else:
                setattr(builtins, name, value)


def _install_call_recording(calls: CallLog) -> tuple[Any, Any]:
    original_open = SnapshotStore.open_attempt
    original_append = Scheduler._append

    def recording_open(self: SnapshotStore, identity: Any) -> Any:
        calls.order.append("authenticate_workspace")
        return original_open(self, identity)

    def recording_append(self: Scheduler, events: Any, *, expected_next_seq: int | None = None) -> None:
        for event in events:
            kind = getattr(event, "kind", None)
            if kind in {"task_attempt_failed", "task_lease_adopted", "task_attempt_started"}:
                calls.order.append(kind)
        return original_append(self, events, expected_next_seq=expected_next_seq)

    SnapshotStore.open_attempt = recording_open  # type: ignore[method-assign]
    Scheduler._append = recording_append  # type: ignore[method-assign]
    return original_open, original_append


def _restore_call_recording(original_open: Any, original_append: Any) -> None:
    SnapshotStore.open_attempt = original_open
    Scheduler._append = original_append


async def _crashed_recoverable_attempt(
    status: _ReconcileStatus,
    tmp_path: Path,
    *,
    expired: bool = True,
    reconcile_error: BaseException | None = None,
    malformed: bool = False,
) -> _RecoveryFixture:
    async def unused(_request: TaskRequest, _context: TaskContext) -> TaskOutcome:
        raise AssertionError("disposable handler must not execute during recovery tests")

    product = _task_product(unused)
    calls = CallLog()
    host = _RecordingHost(
        status=status,
        reconcile_error=reconcile_error,
        malformed=malformed,
        calls=calls,
    )
    start_clock = FakeClock(10.0)
    engine = Engine(tmp_path, clock=start_clock, host=host)
    handle = engine.start(product, entrypoint="main", invocation_id="recover-1")
    ledger = Ledger(handle.invocation_root / "ledger")
    envelopes = ledger.read_all()
    plan = plan_next(product.workflow, fold_events(envelopes))
    if plan.events:
        ledger.append_batch(plan.events, expected_next_seq=envelopes[-1].seq + 1)
    envelopes = ledger.read_all()
    plan = plan_next(product.workflow, fold_events(envelopes))
    assert plan.tasks
    task = plan.tasks[0]
    owner_id = canonical_digest(
        {
            "invocation_id": "recover-1",
            "lock_digest": product.lock_digest,
            "role": "engine-scheduler",
        }
    )
    with handle.workspace as store:
        scheduler = Scheduler(
            _DirectRegistry({"test.empty.run": _RecoverableHandler()}),
            store,
            ledger,
            host,
            owner_id=owner_id,
            clock=start_clock,
            lease_seconds=10.0,
            lock_digest=product.lock_digest,
            composition_digest=product.digest,
            entrypoint="main",
        )
        assert isinstance(_RecoverableHandler(), RecoverableTaskHandler)
        scheduler.start_recoverable(task)
    attempt_before = fold_events(ledger.read_all()).activations[-1].attempts[-1].attempt
    handle.close()
    engine.close()

    original_open, original_append = _install_call_recording(calls)
    try:
        reopen_clock = FakeClock(21.0 if expired else 11.0)
        reopened_engine = Engine(tmp_path, clock=reopen_clock, host=host)
        reopened = reopened_engine.open("recover-1", product)
        calls.order.clear()
        return _RecoveryFixture(
            handle=reopened,
            engine=reopened_engine,
            product=product,
            calls=calls,
            ledger=ledger,
            invocation_id="recover-1",
            attempt_before=attempt_before,
        )
    except BaseException:
        _restore_call_recording(original_open, original_append)
        raise
    finally:
        # Keep patches installed for recover(); caller restores after recover.
        _RecoveryFixture._restore = lambda: _restore_call_recording(original_open, original_append)  # type: ignore[attr-defined]


class _DirectRegistry:
    def __init__(self, handlers: dict[str, TaskHandler]) -> None:
        self.task_handlers = handlers
        self.commit_validators: dict[str, object] = {}
        self.bindings: dict[str, object] = {}


@pytest.mark.parametrize("status, expected", _DECISIONS)
def test_recovery_decision_matrix(tmp_path: Path, status: str, expected: str) -> None:
    asyncio.run(_assert_recovery_decision_matrix(tmp_path, status, expected))  # type: ignore[arg-type]


async def _assert_recovery_decision_matrix(tmp_path: Path, status: _ReconcileStatus, expected: str) -> None:
    fixture = await _crashed_recoverable_attempt(status, tmp_path)
    try:
        result = await fixture.handle.recover()
        assert fixture.calls.order[:2] == ["authenticate_workspace", "reconcile"]
        assert fixture.observed_decision(result) == expected
    finally:
        fixture.handle.close()
        fixture.engine.close()
        _RecoveryFixture._restore()  # type: ignore[attr-defined]


def test_expired_lease_reconciles_before_reclaim_or_adoption(tmp_path: Path) -> None:
    asyncio.run(_assert_expired_lease_order(tmp_path))


async def _assert_expired_lease_order(tmp_path: Path) -> None:
    fixture = await _crashed_recoverable_attempt("running", tmp_path, expired=True)
    try:
        before = fixture.ledger.read_all()
        kinds_before = [item.event.kind for item in before]
        assert "task_attempt_failed" not in kinds_before
        result = await fixture.handle.recover()
        assert fixture.observed_decision(result) == "adopt_same_attempt"
        assert "authenticate_workspace" in fixture.calls.order
        assert "reconcile" in fixture.calls.order
        assert fixture.calls.order.index("authenticate_workspace") < fixture.calls.order.index("reconcile")
        adopt_at = (
            fixture.calls.order.index("task_lease_adopted")
            if "task_lease_adopted" in fixture.calls.order
            else None
        )
        fail_at = (
            fixture.calls.order.index("task_attempt_failed")
            if "task_attempt_failed" in fixture.calls.order
            else None
        )
        reconcile_at = fixture.calls.order.index("reconcile")
        if adopt_at is not None:
            assert reconcile_at < adopt_at
        if fail_at is not None:
            assert reconcile_at < fail_at
        assert "task_attempt_failed" not in fixture.calls.order
        after = fold_events(fixture.ledger.read_all())
        attempt = after.activations[-1].attempts[-1]
        assert attempt.attempt == fixture.attempt_before == 1
        started = [item for item in fixture.ledger.read_all() if item.event.kind == "task_attempt_started"]
        assert len(started) == 1
    finally:
        fixture.handle.close()
        fixture.engine.close()
        _RecoveryFixture._restore()  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "mode",
    [
        "provider_timeout",
        "empty_ambiguous_discovery",
        "missing_formerly_bound",
        "process_disappearance",
        "malformed_response",
        "indeterminate",
    ],
)
def test_indeterminate_recovery_does_not_blindly_retry(tmp_path: Path, mode: str) -> None:
    asyncio.run(_assert_no_blind_retry(tmp_path, mode))


async def _assert_no_blind_retry(tmp_path: Path, mode: str) -> None:
    kwargs: dict[str, Any] = {}
    status: _ReconcileStatus = "indeterminate"
    if mode == "provider_timeout":
        kwargs["reconcile_error"] = TimeoutError("provider timeout")
    elif mode == "malformed_response":
        kwargs["malformed"] = True
    elif mode == "empty_ambiguous_discovery":
        status = "indeterminate"
    elif mode == "missing_formerly_bound":
        status = "indeterminate"
    elif mode == "process_disappearance":
        status = "indeterminate"
    fixture = await _crashed_recoverable_attempt(status, tmp_path, **kwargs)
    try:
        before = fixture.ledger.read_all()
        before_bytes = fixture.ledger.read_bytes()
        workspace_tree = fixture.handle.workspace.head_tree_id()
        result = await fixture.handle.recover()
        assert fixture.observed_decision(result) == "block"
        after = fixture.ledger.read_all()
        assert [item.event.kind for item in after] == [item.event.kind for item in before]
        assert fixture.ledger.read_bytes() == before_bytes
        assert fixture.handle.workspace.head_tree_id() == workspace_tree
        projection = fold_events(after)
        attempt = projection.activations[-1].attempts[-1]
        assert attempt.attempt == fixture.attempt_before == 1
        assert attempt.status == "running"
        assert attempt.activity is not None
        plan = plan_next(fixture.product.workflow, projection)
        assert all(item.kind != "task_attempt_started" for item in plan.events)
        assert all(task.attempt == 1 for task in plan.tasks)
    finally:
        fixture.handle.close()
        fixture.engine.close()
        _RecoveryFixture._restore()  # type: ignore[attr-defined]
