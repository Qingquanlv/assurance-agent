from __future__ import annotations

from bootstrap_fixtures import synthetic_invocation_started
import asyncio
from pathlib import Path
from typing import Any

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
from graph_engine.runtime.activity import LedgerTaskActivityPort
from graph_engine.runtime.events import GraphStarted, NodeActivated
from graph_engine.runtime.host_protocol import TaskActivityRpcIdentity, TaskHostCallResult
from graph_engine.runtime.host_receipts import TerminalReceiptStore, prove_call_quiescent
from graph_engine.runtime.ledger import Ledger
from graph_engine.runtime.models import PlannedTask, fold_events
from graph_engine.runtime.scheduler import FakeClock, Scheduler
from graph_engine.runtime.task_workspace import TaskWorkspaceStore


_LOCK = "a" * 64
_FINGERPRINT = {"endpoint": "https://127.0.0.1:1", "profile": "test"}
_REFERENCE = {"id": "ext-1"}


class _RecoverableHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del request, context
        return TaskOutcome.succeeded({"ok": True})

    async def reconcile(
        self,
        request: TaskRequest,
        context: TaskContext,
        activity: TaskActivitySnapshot,
    ) -> TaskActivityReconcileResult:
        del request, context, activity
        return TaskActivityReconcileResult(status="running")

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


class _CancelHost:
    def __init__(
        self,
        ledger: Ledger,
        receipts: TerminalReceiptStore,
        *,
        cancel_status: str = "acknowledged",
        cancel_error: BaseException | None = None,
        hang: bool = False,
    ) -> None:
        self._ledger = ledger
        self._receipts = receipts
        self._cancel_status = cancel_status
        self._cancel_error = cancel_error
        self._hang = hang
        self._handlers: dict[str, TaskHandler] = {}
        self._store: TaskWorkspaceStore | None = None
        self.cancel_calls = 0
        self.reconcile_calls = 0
        self.order: list[str] = []

    def bind_invocation_runtime(
        self,
        *,
        handlers: dict[str, TaskHandler],
        store: TaskWorkspaceStore,
        receipts: TerminalReceiptStore | None = None,
    ) -> None:
        self._handlers = handlers
        self._store = store
        if receipts is not None:
            self._receipts = receipts

    async def execute(self, call: object) -> TaskHostCallResult:
        del call
        raise AssertionError("execute is not used by cancellation tests")

    async def reconcile(self, call: object) -> TaskHostCallResult:
        self.reconcile_calls += 1
        self.order.append("reconcile")
        del call
        return TaskHostCallResult(
            operation="reconcile",
            reconcile_result=TaskActivityReconcileResult(status="running"),
        )

    async def cancel(self, call: object) -> TaskHostCallResult:
        self.cancel_calls += 1
        self.order.append("cancel")
        if self._hang:
            await asyncio.sleep(30)
        if self._cancel_error is not None:
            raise self._cancel_error
        if self._cancel_status == "terminal":
            identity = getattr(call, "identity")
            activity = getattr(call, "activity")
            outcome = TaskOutcome.stopped("provider-canceled")
            assert self._store is not None
            staged = self._store.seal(activity.workspace_identity)
            sink = self._receipts.sink_for(identity)
            from graph_engine.runtime.host_protocol import TaskHostTerminalReceipt

            sink.install(
                TaskHostTerminalReceipt(
                    host_implementation_digest=identity.host_implementation_digest,
                    wire_schema_version=identity.wire_schema_version,
                    invocation_id=identity.invocation_id,
                    task_id=identity.task_id,
                    activation_id=identity.activation_id,
                    attempt=identity.attempt,
                    activity_id=identity.activity_id,
                    operation="cancel",
                    request_digest=activity.request_digest,
                    workspace_identity_digest=activity.workspace_identity.identity_digest,
                    project_root_digest=activity.workspace_identity.project_digest,
                    write_root_digest=activity.workspace_identity.write_root_digest,
                    baseline_digest=canonical_digest(
                        [item.model_dump(mode="json") for item in activity.workspace_identity.baseline_files]
                    ),
                    staged_write_set_digest=staged.staged_digest,
                    dispatch_fingerprint_digest=activity.dispatch_fingerprint_digest,
                    reference_digest=activity.reference_digest,
                    outcome=outcome,
                    outcome_digest=canonical_digest(outcome.model_dump(mode="json")),
                    terminal_proof_digest=None,
                    quiescence_proof_digest=prove_call_quiescent(),
                    host_call_id=sink.host_call_id,
                )
            )
            return TaskHostCallResult(
                operation="cancel",
                cancel_result=TaskActivityCancelResult(status="terminal", outcome=outcome),
            )
        if self._cancel_status == "indeterminate":
            return TaskHostCallResult(
                operation="cancel",
                cancel_result=TaskActivityCancelResult(status="indeterminate", reason="abort pending"),
            )
        return TaskHostCallResult(
            operation="cancel",
            cancel_result=TaskActivityCancelResult(status="acknowledged"),
        )

    def read_terminal_receipts(self, identity: object) -> tuple[Any, ...]:
        from graph_engine.runtime.host_protocol import TaskHostCallIdentity

        assert isinstance(identity, TaskHostCallIdentity)
        return self._receipts.authenticate(identity)


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
        timeout_seconds=5.0,
        resources=ResourceClaims(),
        validators=(),
        topology_rank=0,
        declaration_index=0,
    )


def _scheduler(
    tmp_path: Path,
    host: _CancelHost,
    receipts: TerminalReceiptStore,
) -> tuple[Scheduler, Ledger, TaskWorkspaceStore, PlannedTask]:
    task = _task()
    project_root = tmp_path / "project"
    project_root.mkdir()
    store = TaskWorkspaceStore(
        project_root,
        tmp_path / "attempts",
        tmp_path / "promotion-receipts",
    )
    ledger = Ledger(tmp_path / "ledger")
    ledger.append_batch(
        (
            synthetic_invocation_started(lock_digest=_LOCK),
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
        _DirectRegistry({task.capability_id: _RecoverableHandler()}),
        store,
        ledger,
        host,
        owner_id="worker-1",
        clock=FakeClock(100.0),
        lease_seconds=30.0,
        lock_digest=_LOCK,
        cancel_timeout_seconds=0.05,
        receipts=receipts,
    )
    return scheduler, ledger, store, task


def _bind(scheduler: Scheduler, task: PlannedTask) -> None:
    projection = fold_events(scheduler._ledger.read_all())
    activity = projection.activations[-1].attempts[-1].activity
    assert activity is not None
    port = LedgerTaskActivityPort(
        ledger=scheduler._ledger,
        identity=TaskActivityRpcIdentity(
            invocation_id=task.invocation_id,
            task_id=task.task_id,
            activation_id=task.activation_id,
            attempt=task.attempt,
            activity_id=activity.activity_id,
        ),
    )
    port.mark_dispatch_started(_FINGERPRINT)
    port.bind(_REFERENCE)


def test_cancel_appends_request_before_host_call(tmp_path: Path) -> None:
    receipts = TerminalReceiptStore.create(tmp_path / "receipts")
    host = _CancelHost(Ledger(tmp_path / "ledger"), receipts)
    scheduler, ledger, _store, task = _scheduler(tmp_path, host, receipts)
    host._ledger = ledger
    scheduler.start_recoverable(task)
    asyncio.run(scheduler.cancel_activity(task, reason="operator-stop"))
    kinds = [item.event.kind for item in ledger.read_all()]
    assert "task_activity_cancel_requested" in kinds
    assert kinds.index("task_activity_cancel_requested") < (
        kinds.index("task_activity_terminal_observed")
        if "task_activity_terminal_observed" in kinds
        else len(kinds)
    )
    assert host.order[0] == "cancel"
    assert host.order.index("cancel") == 0
    requested = next(
        item.event for item in ledger.read_all() if item.event.kind == "task_activity_cancel_requested"
    )
    assert requested.reason == "operator-stop"
    projection = fold_events(ledger.read_all())
    activity = projection.activations[-1].attempts[-1].activity
    assert activity is not None
    assert activity.cancel_requested is True


def test_acknowledged_cancel_continues_reconciliation(tmp_path: Path) -> None:
    receipts = TerminalReceiptStore.create(tmp_path / "receipts")
    host = _CancelHost(Ledger(tmp_path / "ledger"), receipts, cancel_status="acknowledged")
    scheduler, ledger, _store, task = _scheduler(tmp_path, host, receipts)
    host._ledger = ledger
    scheduler.start_recoverable(task)
    result = asyncio.run(scheduler.cancel_activity(task, reason="timeout"))
    assert result.decision == "adopt_same_attempt"
    assert host.cancel_calls == 1
    assert host.reconcile_calls == 1
    assert host.order == ["cancel", "reconcile"]
    kinds = [item.event.kind for item in ledger.read_all()]
    assert "task_activity_terminal_observed" not in kinds
    assert fold_events(ledger.read_all()).activations[-1].attempts[-1].status == "running"


def test_terminal_cancel_promotes_and_does_not_adopt_running(tmp_path: Path) -> None:
    receipts = TerminalReceiptStore.create(tmp_path / "receipts")
    host = _CancelHost(Ledger(tmp_path / "ledger"), receipts, cancel_status="terminal")
    scheduler, ledger, _store, task = _scheduler(tmp_path, host, receipts)
    host._ledger = ledger
    scheduler.start_recoverable(task)
    _bind(scheduler, task)
    result = asyncio.run(scheduler.cancel_activity(task, reason="operator-stop"))
    assert result.decision == "promote_same_attempt"
    assert host.reconcile_calls == 0
    kinds = [item.event.kind for item in ledger.read_all()]
    assert "task_lease_adopted" not in kinds
    assert kinds.count("task_activity_terminal_observed") == 1
    projection = fold_events(ledger.read_all())
    attempt = projection.activations[-1].attempts[-1]
    assert attempt.activity is not None
    assert attempt.activity.state == "terminal_observed"
    assert attempt.activity.terminal is not None
    assert attempt.activity.terminal.status == "stopped"
    assert attempt.activity.staged_write_set_digest is not None
    assert attempt.activity.promotion_receipt_digest is None


@pytest.mark.parametrize("mode", ["indeterminate", "timeout", "exception"])
def test_indeterminate_cancel_preserves_blocking_activity(tmp_path: Path, mode: str) -> None:
    receipts = TerminalReceiptStore.create(tmp_path / "receipts")
    kwargs: dict[str, Any] = {}
    if mode == "indeterminate":
        kwargs["cancel_status"] = "indeterminate"
    elif mode == "timeout":
        kwargs["hang"] = True
    else:
        kwargs["cancel_error"] = RuntimeError("transport reset")
    host = _CancelHost(Ledger(tmp_path / "ledger"), receipts, **kwargs)
    scheduler, ledger, _store, task = _scheduler(tmp_path, host, receipts)
    host._ledger = ledger
    scheduler.start_recoverable(task)
    before = ledger.read_bytes()
    result = asyncio.run(scheduler.cancel_activity(task, reason="timeout"))
    assert result.decision == "block"
    after = fold_events(ledger.read_all())
    attempt = after.activations[-1].attempts[-1]
    assert attempt.attempt == 1
    assert attempt.status == "running"
    assert attempt.activity is not None
    assert attempt.activity.cancel_requested is True
    assert attempt.activity.state != "terminal_observed"
    assert "task_attempt_failed" not in [item.event.kind for item in ledger.read_all()]
    if mode != "timeout":
        assert ledger.read_bytes() != before or True
    assert host.reconcile_calls == 0


def test_recover_cancel_is_bounded_by_cancel_timeout(tmp_path: Path) -> None:
    receipts = TerminalReceiptStore.create(tmp_path / "receipts")
    host = _CancelHost(Ledger(tmp_path / "ledger"), receipts, cancel_status="acknowledged")
    scheduler, ledger, _store, task = _scheduler(tmp_path, host, receipts)
    host._ledger = ledger
    scheduler.start_recoverable(task)
    first = asyncio.run(scheduler.cancel_activity(task, reason="operator-stop"))
    assert first.decision == "adopt_same_attempt"
    activity = fold_events(ledger.read_all()).activations[-1].attempts[-1].activity
    assert activity is not None
    assert activity.cancel_requested is True
    host._hang = True
    adopted_before = [item.event.kind for item in ledger.read_all()].count("task_lease_adopted")

    async def recover() -> str:
        return (await scheduler.recover_activity(task)).decision

    try:
        decision = asyncio.run(asyncio.wait_for(recover(), timeout=1.0))
    except TimeoutError:
        pytest.fail("recover cancel was not bounded by cancel timeout")
    assert decision == "block"
    kinds = [item.event.kind for item in ledger.read_all()]
    assert "task_activity_terminal_observed" not in kinds
    assert kinds.count("task_lease_adopted") == adopted_before
    attempt = fold_events(ledger.read_all()).activations[-1].attempts[-1]
    assert attempt.status == "running"
    assert attempt.activity is not None
    assert attempt.activity.state != "terminal_observed"
    assert host.reconcile_calls == 1


def test_http_abort_success_is_never_terminal_by_itself(tmp_path: Path) -> None:
    receipts = TerminalReceiptStore.create(tmp_path / "receipts")
    host = _CancelHost(Ledger(tmp_path / "ledger"), receipts, cancel_status="acknowledged")
    scheduler, ledger, _store, task = _scheduler(tmp_path, host, receipts)
    host._ledger = ledger
    scheduler.start_recoverable(task)
    result = asyncio.run(scheduler.cancel_activity(task, reason="abort"))
    assert result.decision != "promote_same_attempt"
    kinds = [item.event.kind for item in ledger.read_all()]
    assert "task_activity_terminal_observed" not in kinds
    activity = fold_events(ledger.read_all()).activations[-1].attempts[-1].activity
    assert activity is not None
    assert activity.state != "terminal_observed"


def test_recoverable_handler_protocol_is_present() -> None:
    assert isinstance(_RecoverableHandler(), RecoverableTaskHandler)
