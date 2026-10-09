from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from bootstrap_fixtures import synthetic_invocation_started
from ledger_activity_port import LedgerTaskActivityPort
from graph_engine.attempts.activity import (
    GraphStarted,
    Ledger,
    NodeActivated,
    PlannedTask,
    TaskActivityCancelRequested,
    TaskActivityPrepared,
    TaskActivityTerminalObserved,
    TaskAttemptStarted,
    TaskLeaseAcquired,
    fold_events,
    recovery_decision_for_status,
)
from graph_engine.attempts.execution_host.host_protocol import (
    TaskActivityRpcIdentity,
    TaskHostCallResult,
    current_bound_identity,
)
from graph_engine.attempts.execution_host.host_receipts import TerminalReceiptStore
from graph_engine.attempts.workspace import TaskWorkspaceStore
from graph_engine.canonical import canonical_digest
from graph_engine.evidence.models import activity_id_for_attempt
from graph_engine.plugin_api import (
    RecoverableTaskHandler,
    ResourceClaims,
    TaskActivityCancelResult,
    TaskActivityReconcileResult,
    TaskActivitySnapshot,
    TaskContext,
    TaskOutcome,
    TaskRequest,
)

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
        self._store: TaskWorkspaceStore | None = None
        self.cancel_calls = 0
        self.reconcile_calls = 0
        self.order: list[str] = []

    def bind_store(self, store: TaskWorkspaceStore) -> None:
        self._store = store

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
            activity = getattr(call, "activity")
            outcome = TaskOutcome.stopped("provider-canceled")
            assert self._store is not None
            self._store.seal(activity.workspace_identity)
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


@dataclass(frozen=True, slots=True)
class _Decision:
    decision: str


class _ActivityRuntime:
    def __init__(
        self,
        tmp_path: Path,
        host: _CancelHost,
        receipts: TerminalReceiptStore,
    ) -> None:
        self.task = _task()
        project_root = tmp_path / "project"
        project_root.mkdir()
        self.store = TaskWorkspaceStore(project_root, tmp_path / "attempts", tmp_path / "promotion-receipts")
        self.ledger = Ledger(tmp_path / "ledger")
        self.host = host
        self.receipts = receipts
        self._cancel_timeout_seconds = 0.05
        host.bind_store(self.store)
        host._ledger = self.ledger
        self.ledger.append_batch(
            (
                synthetic_invocation_started(lock_digest=_LOCK),
                GraphStarted(graph_instance_id="graph-1", graph_id="graph-1"),
                NodeActivated(
                    activation_id=self.task.activation_id,
                    graph_instance_id="graph-1",
                    node_id=self.task.node_id,
                    token_ids=(),
                ),
            ),
            expected_next_seq=1,
        )

    def start_recoverable(self) -> None:
        task = self.task
        workspace = self.store.begin(
            task_id=task.task_id,
            attempt=task.attempt,
            output_paths=task.resources.writes,
        )
        activity_id = activity_id_for_attempt(
            task.invocation_id, task.task_id, task.activation_id, task.attempt
        )
        request_digest = canonical_digest(cast_json({"name": "work"}))
        envelopes = self.ledger.read_all()
        self.ledger.append_batch(
            (
                TaskAttemptStarted(
                    activation_id=task.activation_id,
                    attempt=task.attempt,
                    lease_expires_at="130",
                ),
                TaskLeaseAcquired(
                    task_id=task.task_id,
                    activation_id=task.activation_id,
                    attempt=task.attempt,
                    owner_id="worker-1",
                    acquired_at=100.0,
                    heartbeat_at=100.0,
                    expires_at=130.0,
                ),
                TaskActivityPrepared(
                    activity_id=activity_id,
                    task_id=task.task_id,
                    activation_id=task.activation_id,
                    attempt=task.attempt,
                    request_digest=request_digest,
                    workspace_identity=workspace.identity,
                ),
            ),
            expected_next_seq=envelopes[-1].seq + 1,
        )

    def bind(self) -> None:
        projection = fold_events(self.ledger.read_all())
        activity = projection.activations[-1].attempts[-1].activity
        assert activity is not None
        port = LedgerTaskActivityPort(
            ledger=self.ledger,
            identity=TaskActivityRpcIdentity(
                invocation_id=self.task.invocation_id,
                task_id=self.task.task_id,
                activation_id=self.task.activation_id,
                attempt=self.task.attempt,
                activity_id=activity.activity_id,
                **current_bound_identity(  # type: ignore[arg-type]
                    attempt_key_digest="a" * 64,
                    authorization_id="b" * 64,
                    workspace_identity_digest="c" * 64,
                    request_digest="0" * 64,
                    graph_revision="d" * 64,
                    product_lock_digest="a" * 64,
                    handler_id="test.echo.run",
                ),
            ),
        )
        port.mark_dispatch_started(_FINGERPRINT)
        port.bind(_REFERENCE)

    def _live_activity(self):
        projection = fold_events(self.ledger.read_all())
        activity = projection.activations[-1].attempts[-1].activity
        assert activity is not None
        return activity

    async def cancel_activity(self, *, reason: str) -> _Decision:
        activity = self._live_activity()
        if not activity.cancel_requested:
            envelopes = self.ledger.read_all()
            self.ledger.append_batch(
                (
                    TaskActivityCancelRequested(
                        activity_id=activity.activity_id,
                        reason=reason,
                        requested_at=100.0,
                    ),
                ),
                expected_next_seq=envelopes[-1].seq + 1,
            )
            activity = self._live_activity()
        identity = TaskActivityRpcIdentity(
            invocation_id=self.task.invocation_id,
            task_id=self.task.task_id,
            activation_id=self.task.activation_id,
            attempt=self.task.attempt,
            activity_id=activity.activity_id,
            **current_bound_identity(  # type: ignore[arg-type]
                attempt_key_digest="a" * 64,
                authorization_id="b" * 64,
                workspace_identity_digest="c" * 64,
                request_digest="0" * 64,
                graph_revision="d" * 64,
                product_lock_digest="a" * 64,
                handler_id="test.echo.run",
            ),
        )
        call = type("CancelCall", (), {"identity": identity, "activity": activity})()
        try:
            async with asyncio.timeout(self._cancel_timeout_seconds):
                result = await self.host.cancel(call)
        except TimeoutError:
            return _Decision("block")
        except Exception:
            return _Decision("block")
        status = result.cancel_result.status if result.cancel_result is not None else "indeterminate"
        if status == "indeterminate":
            return _Decision("block")
        if status == "terminal":
            activity = self._live_activity()
            outcome = result.cancel_result.outcome
            assert outcome is not None
            staged = self.store.seal(activity.workspace_identity)
            envelopes = self.ledger.read_all()
            self.ledger.append_batch(
                (
                    TaskActivityTerminalObserved(
                        activity_id=activity.activity_id,
                        outcome=outcome,
                        outcome_digest=canonical_digest(outcome.model_dump(mode="json")),
                        terminal_proof_digest=None,
                        staged_write_set_digest=staged.staged_digest,
                    ),
                ),
                expected_next_seq=envelopes[-1].seq + 1,
            )
            return _Decision(recovery_decision_for_status("terminal"))
        await self.host.reconcile(call)
        return _Decision(recovery_decision_for_status("running"))

    async def recover_activity(self) -> _Decision:
        activity = self._live_activity()
        if activity.cancel_requested:
            return await self.cancel_activity(reason="operator-stop")
        return _Decision(recovery_decision_for_status("running"))


def cast_json(value: dict[str, str]) -> dict[str, str]:
    return value


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


def _runtime(tmp_path: Path, host: _CancelHost, receipts: TerminalReceiptStore) -> _ActivityRuntime:
    return _ActivityRuntime(tmp_path, host, receipts)


def test_cancel_appends_request_before_host_call(tmp_path: Path) -> None:
    receipts = TerminalReceiptStore.create(tmp_path / "receipts")
    host = _CancelHost(Ledger(tmp_path / "unused"), receipts)
    runtime = _runtime(tmp_path, host, receipts)
    runtime.start_recoverable()
    asyncio.run(runtime.cancel_activity(reason="operator-stop"))
    kinds = [item.event.kind for item in runtime.ledger.read_all()]
    assert "task_activity_cancel_requested" in kinds
    assert kinds.index("task_activity_cancel_requested") < (
        kinds.index("task_activity_terminal_observed")
        if "task_activity_terminal_observed" in kinds
        else len(kinds)
    )
    assert host.order[0] == "cancel"
    requested = next(
        item.event
        for item in runtime.ledger.read_all()
        if item.event.kind == "task_activity_cancel_requested"
    )
    assert requested.reason == "operator-stop"
    projection = fold_events(runtime.ledger.read_all())
    activity = projection.activations[-1].attempts[-1].activity
    assert activity is not None
    assert activity.cancel_requested is True


def test_acknowledged_cancel_continues_reconciliation(tmp_path: Path) -> None:
    receipts = TerminalReceiptStore.create(tmp_path / "receipts")
    host = _CancelHost(Ledger(tmp_path / "unused"), receipts, cancel_status="acknowledged")
    runtime = _runtime(tmp_path, host, receipts)
    runtime.start_recoverable()
    result = asyncio.run(runtime.cancel_activity(reason="timeout"))
    assert result.decision == "adopt_same_attempt"
    assert host.cancel_calls == 1
    assert host.reconcile_calls == 1
    assert host.order == ["cancel", "reconcile"]
    kinds = [item.event.kind for item in runtime.ledger.read_all()]
    assert "task_activity_terminal_observed" not in kinds
    assert fold_events(runtime.ledger.read_all()).activations[-1].attempts[-1].status == "running"


def test_terminal_cancel_promotes_and_does_not_adopt_running(tmp_path: Path) -> None:
    receipts = TerminalReceiptStore.create(tmp_path / "receipts")
    host = _CancelHost(Ledger(tmp_path / "unused"), receipts, cancel_status="terminal")
    runtime = _runtime(tmp_path, host, receipts)
    runtime.start_recoverable()
    runtime.bind()
    result = asyncio.run(runtime.cancel_activity(reason="operator-stop"))
    assert result.decision == "promote_same_attempt"
    assert host.reconcile_calls == 0
    kinds = [item.event.kind for item in runtime.ledger.read_all()]
    assert "task_lease_adopted" not in kinds
    assert kinds.count("task_activity_terminal_observed") == 1
    projection = fold_events(runtime.ledger.read_all())
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
    host = _CancelHost(Ledger(tmp_path / "unused"), receipts, **kwargs)
    runtime = _runtime(tmp_path, host, receipts)
    runtime.start_recoverable()
    result = asyncio.run(runtime.cancel_activity(reason="timeout"))
    assert result.decision == "block"
    after = fold_events(runtime.ledger.read_all())
    attempt = after.activations[-1].attempts[-1]
    assert attempt.attempt == 1
    assert attempt.status == "running"
    assert attempt.activity is not None
    assert attempt.activity.cancel_requested is True
    assert attempt.activity.state != "terminal_observed"
    assert "task_attempt_failed" not in [item.event.kind for item in runtime.ledger.read_all()]
    assert host.reconcile_calls == 0


def test_recover_cancel_is_bounded_by_cancel_timeout(tmp_path: Path) -> None:
    receipts = TerminalReceiptStore.create(tmp_path / "receipts")
    host = _CancelHost(Ledger(tmp_path / "unused"), receipts, cancel_status="acknowledged")
    runtime = _runtime(tmp_path, host, receipts)
    runtime.start_recoverable()
    first = asyncio.run(runtime.cancel_activity(reason="operator-stop"))
    assert first.decision == "adopt_same_attempt"
    activity = fold_events(runtime.ledger.read_all()).activations[-1].attempts[-1].activity
    assert activity is not None
    assert activity.cancel_requested is True
    host._hang = True
    adopted_before = [item.event.kind for item in runtime.ledger.read_all()].count("task_lease_adopted")

    async def recover() -> str:
        return (await runtime.recover_activity()).decision

    try:
        decision = asyncio.run(asyncio.wait_for(recover(), timeout=1.0))
    except TimeoutError:
        pytest.fail("recover cancel was not bounded by cancel timeout")
    assert decision == "block"
    kinds = [item.event.kind for item in runtime.ledger.read_all()]
    assert "task_activity_terminal_observed" not in kinds
    assert kinds.count("task_lease_adopted") == adopted_before
    attempt = fold_events(runtime.ledger.read_all()).activations[-1].attempts[-1]
    assert attempt.status == "running"
    assert attempt.activity is not None
    assert attempt.activity.state != "terminal_observed"
    assert host.reconcile_calls == 1


def test_http_abort_success_is_never_terminal_by_itself(tmp_path: Path) -> None:
    receipts = TerminalReceiptStore.create(tmp_path / "receipts")
    host = _CancelHost(Ledger(tmp_path / "unused"), receipts, cancel_status="acknowledged")
    runtime = _runtime(tmp_path, host, receipts)
    runtime.start_recoverable()
    result = asyncio.run(runtime.cancel_activity(reason="abort"))
    assert result.decision != "promote_same_attempt"
    kinds = [item.event.kind for item in runtime.ledger.read_all()]
    assert "task_activity_terminal_observed" not in kinds
    activity = fold_events(runtime.ledger.read_all()).activations[-1].attempts[-1].activity
    assert activity is not None
    assert activity.state != "terminal_observed"


def test_recoverable_handler_protocol_is_present() -> None:
    assert isinstance(_RecoverableHandler(), RecoverableTaskHandler)
