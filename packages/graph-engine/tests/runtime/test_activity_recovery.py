from __future__ import annotations

import asyncio
import builtins
import importlib.util
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

import pytest

from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.plugin_api import (
    InvocationWorkspaceBinding,
    RecoverableTaskHandler,
    TaskActivityCancelResult,
    TaskActivityReconcileResult,
    TaskActivitySnapshot,
    TaskContext,
    TaskHandler,
    TaskOutcome,
    TaskRequest,
)
from graph_engine.runtime.activity import LedgerTaskActivityPort
from graph_engine.runtime.events import TaskActivityDispatchStarted
from graph_engine.runtime.secret_sources import empty_runtime_authorization
from graph_engine.runtime.seed import empty_invocation_seed
from graph_engine.runtime.engine import Engine, EngineConflictError
from graph_engine.runtime.host_protocol import TaskHostCallResult, TaskHostTerminalReceipt
from graph_engine.runtime.host_receipts import TerminalReceiptStore, prove_call_quiescent
from graph_engine.runtime.ledger import Ledger
from graph_engine.runtime.models import fold_events
from graph_engine.runtime.planner import plan_next
from graph_engine.runtime.scheduler import FakeClock, Scheduler
from graph_engine.runtime.task_workspace import TaskWorkspaceStore


_ReconcileStatus = Literal["not_dispatched", "running", "terminal", "absent", "indeterminate"]
_DECISIONS = (
    ("not_dispatched", "execute_same_attempt"),
    ("running", "adopt_same_attempt"),
    ("terminal", "block"),
    ("absent", "block"),
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
        self._store: TaskWorkspaceStore | None = None
        self._receipts: TerminalReceiptStore | None = None

    def bind_invocation_runtime(
        self,
        *,
        handlers: dict[str, TaskHandler],
        store: TaskWorkspaceStore,
        receipts: object | None = None,
    ) -> None:
        self._handlers = handlers
        self._store = store
        if receipts is not None:
            self._receipts = receipts

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
    for attribute, name in (
        ("_CALLBACKS", names[0]),
        ("_EFFECT_CALLBACKS", names[1]),
        ("_EFFECT_POLICIES", names[2]),
    ):
        existing = saved[name]
        if isinstance(existing, dict):
            setattr(module, attribute, existing)
            setattr(builtins, name, existing)
    module._restored_builtins = saved  # type: ignore[attr-defined]
    return module


def _workspace_binding(root: Path) -> InvocationWorkspaceBinding:
    project_root = root / "project"
    attempts_root = root / "attempts"
    receipts_root = root / "promotion-receipts"
    for path in (project_root, attempts_root, receipts_root):
        path.mkdir(exist_ok=True)
    return InvocationWorkspaceBinding(
        project_root=project_root,
        attempts_root=attempts_root,
        receipts_root=receipts_root,
    )


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
    original_open = TaskWorkspaceStore.begin
    original_append = Scheduler._append

    def recording_open(self: TaskWorkspaceStore, *args: Any, **kwargs: Any) -> Any:
        calls.order.append("authenticate_workspace")
        return original_open(self, *args, **kwargs)

    def recording_append(self: Scheduler, events: Any, *, expected_next_seq: int | None = None) -> None:
        for event in events:
            kind = getattr(event, "kind", None)
            if kind in {"task_attempt_failed", "task_lease_adopted", "task_attempt_started"}:
                calls.order.append(kind)
        return original_append(self, events, expected_next_seq=expected_next_seq)

    TaskWorkspaceStore.begin = recording_open  # type: ignore[method-assign]
    Scheduler._append = recording_append  # type: ignore[method-assign]
    return original_open, original_append


def _restore_call_recording(original_open: Any, original_append: Any) -> None:
    TaskWorkspaceStore.begin = original_open
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
    workspace_binding = _workspace_binding(tmp_path)
    engine = Engine(tmp_path, clock=start_clock, host=host)
    handle = engine.start(
        product,
        entrypoint="main",
        invocation_id="recover-1",
        seed=empty_invocation_seed(),
        authorization=empty_runtime_authorization(),
        workspace_binding=workspace_binding,
    )
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
    with closing(handle.workspace) as store:
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
        reopened = reopened_engine.open(
            "recover-1",
            product,
            authorization=empty_runtime_authorization(),
            workspace_binding=workspace_binding,
        )
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
        if status in {"terminal", "absent"}:
            kinds = [item.event.kind for item in fixture.ledger.read_all()]
            assert "task_activity_terminal_observed" not in kinds
            assert "task_attempt_failed" not in kinds
            attempt = fold_events(fixture.ledger.read_all()).activations[-1].attempts[-1]
            assert attempt.status == "running"
            assert attempt.activity is not None
            assert attempt.activity.state != "terminal_observed"
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
        with closing(fixture.handle.workspace) as workspace:
            project_entries = tuple(
                sorted(path.relative_to(workspace.project_root) for path in workspace.project_root.rglob("*"))
            )
        result = await fixture.handle.recover()
        assert fixture.observed_decision(result) == "block"
        after = fixture.ledger.read_all()
        assert [item.event.kind for item in after] == [item.event.kind for item in before]
        assert fixture.ledger.read_bytes() == before_bytes
        with closing(fixture.handle.workspace) as workspace:
            assert (
                tuple(
                    sorted(
                        path.relative_to(workspace.project_root) for path in workspace.project_root.rglob("*")
                    )
                )
                == project_entries
            )
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


_NON_ADOPTED = (
    ("terminal", "block"),
    ("absent", "block"),
    ("indeterminate", "block"),
)


@pytest.mark.parametrize("status, expected", _NON_ADOPTED)
def test_expired_non_adopted_recovery_does_not_conflict_on_resume(
    tmp_path: Path,
    status: str,
    expected: str,
) -> None:
    fixture = asyncio.run(_crashed_recoverable_attempt(status, tmp_path, expired=True))  # type: ignore[arg-type]
    try:
        result = asyncio.run(fixture.handle.recover())
        assert fixture.observed_decision(result) == expected
        assert "task_lease_adopted" not in fixture.calls.order
        assert "task_attempt_started" not in fixture.calls.order
        try:
            run_result = fixture.engine.run_until_blocked(fixture.handle)
        except EngineConflictError as error:
            raise AssertionError("expired non-adopted recovery must not look like a rival runner") from error
        assert run_result.status == "interrupted"
        assert run_result.terminal_reason == "activity_recovery"
        after = fold_events(fixture.ledger.read_all())
        attempt = after.activations[-1].attempts[-1]
        assert attempt.attempt == fixture.attempt_before == 1
        assert attempt.status == "running"
        assert attempt.activity is not None
        started = [item for item in fixture.ledger.read_all() if item.event.kind == "task_attempt_started"]
        assert len(started) == 1
        assert "task_attempt_failed" not in [item.event.kind for item in fixture.ledger.read_all()]
    finally:
        fixture.handle.close()
        fixture.engine.close()
        _RecoveryFixture._restore()  # type: ignore[attr-defined]


def test_open_after_recovery_defers_compiled_events(tmp_path: Path) -> None:
    asyncio.run(_assert_open_after_recovery_defers_compiled_events(tmp_path))


async def _assert_open_after_recovery_defers_compiled_events(tmp_path: Path) -> None:
    helpers = _load_engine_helpers()
    try:
        product = helpers._parallel_task_product()
        in_process_host_cls = helpers._InProcessTestHost
        function_handler_cls = helpers._FunctionHandler
    finally:
        for name, value in helpers._restored_builtins.items():
            if value is None:
                if hasattr(builtins, name):
                    delattr(builtins, name)
            else:
                setattr(builtins, name, value)

    async def succeed(_request: TaskRequest, _context: TaskContext) -> TaskOutcome:
        return TaskOutcome.succeeded({"ok": True})

    calls = CallLog()
    host = _RecordingHost(status="running", calls=calls)
    start_clock = FakeClock(10.0)
    workspace_binding = _workspace_binding(tmp_path)
    engine = Engine(tmp_path, clock=start_clock, host=host)
    handle = engine.start(
        product,
        entrypoint="main",
        invocation_id="adopt-defer",
        seed=empty_invocation_seed(),
        authorization=empty_runtime_authorization(),
        workspace_binding=workspace_binding,
    )
    ledger = Ledger(handle.invocation_root / "ledger")
    envelopes = ledger.read_all()
    structural = plan_next(product.workflow, fold_events(envelopes))
    ledger.append_batch(structural.events, expected_next_seq=envelopes[-1].seq + 1)
    tasks = plan_next(product.workflow, fold_events(ledger.read_all())).tasks
    recoverable = next(task for task in tasks if task.node_id == "cause")
    sibling = next(task for task in tasks if task.node_id == "sibling")
    owner_id = canonical_digest(
        {
            "invocation_id": "adopt-defer",
            "lock_digest": product.lock_digest,
            "role": "engine-scheduler",
        }
    )
    with closing(handle.workspace) as store:
        recover_scheduler = Scheduler(
            _DirectRegistry({recoverable.capability_id: _RecoverableHandler()}),
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
        recover_scheduler.start_recoverable(recoverable)
        sibling_scheduler = Scheduler(
            _DirectRegistry({sibling.capability_id: function_handler_cls(succeed)}),
            store,
            ledger,
            in_process_host_cls(),
            owner_id=owner_id,
            clock=start_clock,
            lease_seconds=10.0,
            lock_digest=product.lock_digest,
            composition_digest=product.digest,
            entrypoint="main",
        )
        finalized = await sibling_scheduler.run_wave((sibling,))
        assert len(finalized) == 1
        assert finalized[0].outcome.status == "succeeded"

    due = plan_next(product.workflow, fold_events(ledger.read_all()))
    assert due.events
    assert any(
        event.kind == "node_completed" and getattr(event, "activation_id", None) == sibling.activation_id
        for event in due.events
    )
    assert all(event.kind != "task_lease_adopted" for event in due.events)

    try:
        result = await handle.recover()
        decisions = getattr(result, "decisions", ())
        assert len(decisions) == 1
        assert decisions[0].decision == "adopt_same_attempt"
        envelopes = ledger.read_all()
        kinds = [item.event.kind for item in envelopes]
        assert "task_lease_adopted" in kinds
        assert not any(
            item.event.kind == "node_completed"
            and getattr(item.event, "activation_id", None) == sibling.activation_id
            for item in envelopes
        )
        handle.close()
        engine.close()
        reopened_engine = Engine(tmp_path, clock=FakeClock(11.0), host=host)
        reopened = reopened_engine.open(
            "adopt-defer",
            product,
            authorization=empty_runtime_authorization(),
            workspace_binding=workspace_binding,
        )
        reopened.close()
        reopened_engine.close()
    finally:
        handle.close()
        engine.close()


def test_checkpoints_and_effects_cannot_authorize_activity(tmp_path: Path) -> None:
    asyncio.run(_assert_checkpoints_and_effects_cannot_authorize(tmp_path))


async def _assert_checkpoints_and_effects_cannot_authorize(tmp_path: Path) -> None:
    fixture = await _crashed_recoverable_attempt("running", tmp_path, expired=False)
    try:
        first = await fixture.handle.recover()
        first_decision = fixture.observed_decision(first)
        first_calls = list(fixture.calls.order)
        checkpoint = fixture.handle.invocation_root / "checkpoint.json"
        if checkpoint.exists():
            checkpoint.unlink()
        second = await fixture.handle.recover()
        assert fixture.observed_decision(second) == first_decision
        extra = fixture.calls.order[len(first_calls) :]
        assert "task_attempt_started" not in extra
        from graph_engine.runtime.events import EffectIntentCommitted
        from graph_engine.runtime.ledger import append_validated_batch
        from graph_engine.runtime.models import ProjectionError

        with pytest.raises((ProjectionError, ValueError, TypeError)):
            append_validated_batch(
                fixture.ledger,
                (
                    EffectIntentCommitted(
                        effect_id="effect-activity",
                        activation_id=fold_events(fixture.ledger.read_all()).activations[-1].activation_id,
                        attempt=1,
                        index=0,
                        effect_kind="test.empty.intent",
                        payload={"create": True, "bind": True, "cancel": True, "terminal": True},
                        idempotency_key="0" * 64,
                    ),
                ),
                expected_next_seq=fixture.ledger.read_all()[-1].seq + 1,
            )
    finally:
        fixture.handle.close()
        fixture.engine.close()
        _RecoveryFixture._restore()  # type: ignore[attr-defined]


class _TerminalCancelHost(_RecordingHost):
    async def cancel(self, call: object) -> TaskHostCallResult:
        self.calls.order.append("cancel")
        identity = getattr(call, "identity")
        activity = getattr(call, "activity")
        outcome = TaskOutcome.stopped("provider-canceled")
        assert self._receipts is not None
        assert self._store is not None
        staged = self._store.seal(activity.workspace_identity)
        sink = self._receipts.sink_for(identity)
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
            cancel_result=TaskActivityCancelResult(
                status="terminal",
                outcome=outcome,
            ),
        )


def test_cancel_terminal_does_not_fall_through_to_running_adoption(tmp_path: Path) -> None:
    asyncio.run(_assert_cancel_terminal_does_not_adopt(tmp_path))


async def _assert_cancel_terminal_does_not_adopt(tmp_path: Path) -> None:
    async def unused(_request: TaskRequest, _context: TaskContext) -> TaskOutcome:
        raise AssertionError("disposable handler must not execute")

    product = _task_product(unused)
    calls = CallLog()
    host = _TerminalCancelHost(status="running", calls=calls)
    start_clock = FakeClock(10.0)
    workspace_binding = _workspace_binding(tmp_path)
    engine = Engine(tmp_path, clock=start_clock, host=host)
    handle = engine.start(
        product,
        entrypoint="main",
        invocation_id="cancel-term",
        seed=empty_invocation_seed(),
        authorization=empty_runtime_authorization(),
        workspace_binding=workspace_binding,
    )
    ledger = Ledger(handle.invocation_root / "ledger")
    envelopes = ledger.read_all()
    plan = plan_next(product.workflow, fold_events(envelopes))
    if plan.events:
        ledger.append_batch(plan.events, expected_next_seq=envelopes[-1].seq + 1)
    task = plan_next(product.workflow, fold_events(ledger.read_all())).tasks[0]
    owner_id = canonical_digest(
        {
            "invocation_id": "cancel-term",
            "lock_digest": product.lock_digest,
            "role": "engine-scheduler",
        }
    )
    with closing(handle.workspace) as store:
        receipts = TerminalReceiptStore.open_or_create(handle.invocation_root / "receipts")
        host._receipts = receipts
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
            receipts=receipts,
        )
        scheduler.start_recoverable(task)
        await scheduler.cancel_activity(task, reason="operator-stop")
    kinds_after_cancel = [item.event.kind for item in ledger.read_all()]
    assert "task_activity_terminal_observed" in kinds_after_cancel
    assert "task_attempt_stopped" in kinds_after_cancel
    assert "task_lease_adopted" not in kinds_after_cancel
    handle.close()
    engine.close()
    original_open, original_append = _install_call_recording(calls)
    reopened_engine = None
    reopened = None
    try:
        reopened_engine = Engine(tmp_path, clock=FakeClock(11.0), host=host)
        reopened = reopened_engine.open(
            "cancel-term",
            product,
            authorization=empty_runtime_authorization(),
            workspace_binding=workspace_binding,
        )
        calls.order.clear()
        result = await reopened.recover()
        decisions = getattr(result, "decisions", ())
        assert decisions == ()
        assert "task_lease_adopted" not in calls.order
        kinds = [item.event.kind for item in ledger.read_all()]
        assert "task_lease_adopted" not in kinds
        attempt = fold_events(ledger.read_all()).activations[-1].attempts[-1]
        assert attempt.status == "stopped"
        assert attempt.activity is not None
        assert attempt.activity.state == "terminal_observed"
    finally:
        if reopened is not None:
            reopened.close()
        if reopened_engine is not None:
            reopened_engine.close()
        _restore_call_recording(original_open, original_append)


class _PreparedDispatchHost:
    """Reconcile stays blocked; execute records the same-attempt dispatch."""

    _FINGERPRINT = {"endpoint": "https://127.0.0.1:1", "profile": "test"}
    _REFERENCE = {"id": "ext-prepared"}

    def __init__(self) -> None:
        self.execute_calls: list[Any] = []
        self._handlers: dict[str, TaskHandler] = {}
        self._store: TaskWorkspaceStore | None = None
        self._receipts: TerminalReceiptStore | None = None
        self._ledger: Ledger | None = None

    def bind_ledger(self, ledger: Ledger) -> None:
        self._ledger = ledger

    def bind_invocation_runtime(
        self,
        *,
        handlers: dict[str, TaskHandler],
        store: TaskWorkspaceStore,
        receipts: object | None = None,
        handler_import_roots: object | None = None,
    ) -> None:
        del handler_import_roots
        self._handlers = handlers
        self._store = store
        if receipts is not None:
            self._receipts = receipts

    async def execute(self, call: object) -> TaskHostCallResult:
        self.execute_calls.append(call)
        assert self._store is not None
        identity = getattr(call, "identity")
        request = getattr(call, "request")
        attempt_root = getattr(call, "attempt_root")
        activity_rpc = getattr(call, "activity_rpc")
        handler = self._handlers[request.capability_id]
        workspace = self._store.begin(
            task_id=attempt_root.workspace_identity.task_id,
            attempt=attempt_root.workspace_identity.attempt,
            output_paths=attempt_root.workspace_identity.output_paths,
        )
        assert workspace.identity == attempt_root.workspace_identity
        port = None
        if activity_rpc.activity_id is not None and self._ledger is not None:
            port = LedgerTaskActivityPort(ledger=self._ledger, identity=activity_rpc)
            port.mark_dispatch_started(cast(JSONValue, self._FINGERPRINT))
            port.bind(cast(JSONValue, self._REFERENCE))
        outcome = await handler.execute(
            request,
            TaskContext(
                project_root=workspace.project_root,
                write_root=workspace.write_root,
                workspace_identity=workspace.identity,
                heartbeat=lambda: None,
                cancel_requested=lambda: False,
                invocation=request.invocation,
                activity=port,
            ),
        )
        if port is not None:
            self._install_receipt(identity, port.snapshot, outcome)
        return TaskHostCallResult(operation="execute", outcome=outcome)

    async def reconcile(self, call: object) -> TaskHostCallResult:
        del call
        return TaskHostCallResult(
            operation="reconcile",
            reconcile_result=TaskActivityReconcileResult(
                status="indeterminate",
                reason="prepared undispatched fixture does not adopt",
            ),
        )

    async def cancel(self, call: object) -> TaskHostCallResult:
        del call
        return TaskHostCallResult(
            operation="cancel",
            cancel_result=TaskActivityCancelResult(
                status="indeterminate",
                reason="prepared undispatched fixture does not cancel",
            ),
        )

    def read_terminal_receipts(self, identity: object) -> tuple[TaskHostTerminalReceipt, ...]:
        if self._receipts is None:
            return ()
        return self._receipts.authenticate(identity)

    def _install_receipt(
        self,
        identity: Any,
        activity: TaskActivitySnapshot,
        outcome: TaskOutcome,
    ) -> None:
        if self._receipts is None or identity.activity_id is None:
            return
        assert self._store is not None
        sink = self._receipts.sink_for(identity)
        sink.install(
            TaskHostTerminalReceipt(
                host_implementation_digest=identity.host_implementation_digest,
                wire_schema_version=identity.wire_schema_version,
                invocation_id=identity.invocation_id,
                task_id=identity.task_id,
                activation_id=identity.activation_id,
                attempt=identity.attempt,
                activity_id=identity.activity_id,
                operation=identity.operation,
                request_digest=activity.request_digest,
                workspace_identity_digest=activity.workspace_identity.identity_digest,
                project_root_digest=activity.workspace_identity.project_digest,
                write_root_digest=activity.workspace_identity.write_root_digest,
                baseline_digest=canonical_digest(
                    [item.model_dump(mode="json") for item in activity.workspace_identity.baseline_files]
                ),
                staged_write_set_digest=self._store.seal(activity.workspace_identity).staged_digest,
                dispatch_fingerprint_digest=activity.dispatch_fingerprint_digest,
                reference_digest=activity.reference_digest,
                outcome=outcome,
                outcome_digest=canonical_digest(outcome.model_dump(mode="json")),
                terminal_proof_digest=None,
                quiescence_proof_digest=prove_call_quiescent(),
                host_call_id=sink.host_call_id,
            )
        )


def _recoverable_product() -> Any:
    helpers = _load_engine_helpers()
    try:
        return helpers._recoverable_task_product(_RecoverableHandler())
    finally:
        for name, value in helpers._restored_builtins.items():
            if value is None:
                if hasattr(builtins, name):
                    delattr(builtins, name)
            else:
                setattr(builtins, name, value)


def _persist_prepared_attempt(
    tmp_path: Path,
    host: _PreparedDispatchHost,
    *,
    dispatched: bool = False,
) -> _RecoveryFixture:
    product = _recoverable_product()
    start_clock = FakeClock(10.0)
    workspace_binding = _workspace_binding(tmp_path)
    engine = Engine(tmp_path, clock=start_clock, host=host)
    handle = engine.start(
        product,
        entrypoint="main",
        invocation_id="recover-prepared",
        seed=empty_invocation_seed(),
        authorization=empty_runtime_authorization(),
        workspace_binding=workspace_binding,
    )
    ledger = Ledger(handle.invocation_root / "ledger")
    host.bind_ledger(ledger)
    envelopes = ledger.read_all()
    plan = plan_next(product.workflow, fold_events(envelopes))
    if plan.events:
        ledger.append_batch(plan.events, expected_next_seq=envelopes[-1].seq + 1)
    task = plan_next(product.workflow, fold_events(ledger.read_all())).tasks[0]
    owner_id = canonical_digest(
        {
            "invocation_id": "recover-prepared",
            "lock_digest": product.lock_digest,
            "role": "engine-scheduler",
        }
    )
    with closing(handle.workspace) as store:
        scheduler = Scheduler(
            product.registries.capabilities,
            store,
            ledger,
            host,
            owner_id=owner_id,
            clock=start_clock,
            lease_seconds=30.0,
            lock_digest=product.lock_digest,
            composition_digest=product.digest,
            entrypoint="main",
        )
        scheduler.start_recoverable(task)
        if dispatched:
            attempt = fold_events(ledger.read_all()).activations[-1].attempts[-1]
            assert attempt.activity is not None
            fingerprint = {"endpoint": "https://provider.example", "session": "already-live"}
            ledger.append_batch(
                (
                    TaskActivityDispatchStarted(
                        activity_id=attempt.activity.activity_id,
                        dispatch_fingerprint=fingerprint,
                        dispatch_fingerprint_digest=canonical_digest(fingerprint),
                    ),
                ),
                expected_next_seq=ledger.read_all()[-1].seq + 1,
            )
    attempt_before = fold_events(ledger.read_all()).activations[-1].attempts[-1].attempt
    handle.close()
    engine.close()
    reopened_engine = Engine(tmp_path, clock=FakeClock(11.0), host=host)
    reopened = reopened_engine.open(
        "recover-prepared",
        product,
        authorization=empty_runtime_authorization(),
        workspace_binding=workspace_binding,
    )
    host.bind_ledger(Ledger(reopened.invocation_root / "ledger"))
    return _RecoveryFixture(
        handle=reopened,
        engine=reopened_engine,
        product=product,
        calls=CallLog(),
        ledger=ledger,
        invocation_id="recover-prepared",
        attempt_before=attempt_before,
    )


def test_prepared_undispatched_resume_dispatches_same_attempt(tmp_path: Path) -> None:
    host = _PreparedDispatchHost()
    fixture = _persist_prepared_attempt(tmp_path, host)
    try:
        before = fixture.ledger.read_all()
        attempt = fold_events(before).activations[-1].attempts[-1]
        assert attempt.status == "running"
        assert attempt.activity is not None
        assert attempt.activity.state == "prepared"
        assert attempt.activity.dispatch_fingerprint is None
        assert attempt.activity.dispatch_fingerprint_digest is None
        assert attempt.activity.reference is None
        activity_id = attempt.activity.activity_id
        workspace_identity = attempt.activity.workspace_identity
        result = fixture.engine.run_until_blocked(fixture.handle)
        after = fixture.ledger.read_all()
        kinds = [item.event.kind for item in after]
        assert result.terminal_reason != "activity_recovery"
        assert result.status == "succeeded"
        assert len(after) > len(before)
        assert len(host.execute_calls) == 1
        execute_call = host.execute_calls[0]
        assert execute_call.identity.activity_id == activity_id
        assert execute_call.identity.attempt == fixture.attempt_before == 1
        assert execute_call.attempt_root.workspace_identity == workspace_identity
        assert "task_activity_dispatch_started" in kinds
        started = [item for item in after if item.event.kind == "task_attempt_started"]
        assert len(started) == 1
        prepared = [item for item in after if item.event.kind == "task_activity_prepared"]
        assert len(prepared) == 1
        assert prepared[0].event.activity_id == activity_id
        assert prepared[0].event.workspace_identity == workspace_identity
    finally:
        fixture.handle.close()
        fixture.engine.close()


def test_already_dispatched_live_activity_still_yields_activity_recovery(tmp_path: Path) -> None:
    host = _PreparedDispatchHost()
    fixture = _persist_prepared_attempt(tmp_path, host, dispatched=True)
    try:
        before = fixture.ledger.read_all()
        attempt = fold_events(before).activations[-1].attempts[-1]
        assert attempt.status == "running"
        assert attempt.activity is not None
        assert attempt.activity.state == "dispatch_started"
        assert attempt.activity.dispatch_fingerprint_digest is not None
        result = fixture.engine.run_until_blocked(fixture.handle)
        after = fixture.ledger.read_all()
        assert result.status == "interrupted"
        assert result.terminal_reason == "activity_recovery"
        assert host.execute_calls == []
        assert [item.event.kind for item in after] == [item.event.kind for item in before]
        refreshed = fold_events(after).activations[-1].attempts[-1]
        assert refreshed.attempt == fixture.attempt_before == 1
        assert refreshed.status == "running"
        assert refreshed.activity is not None
        assert refreshed.activity.state == "dispatch_started"
    finally:
        fixture.handle.close()
        fixture.engine.close()
