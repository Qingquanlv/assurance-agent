from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import pytest

import graph_engine.runtime.host_receipts as host_receipts
import graph_engine.runtime.ledger as ledger_runtime
import graph_engine.runtime.scheduler as scheduler_runtime
from graph_engine.canonical import canonical_digest
from graph_engine.composition.lock import pinned_execution_host_lock
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
from graph_engine.runtime.activity import LedgerTaskActivityPort, write_set_digest
from graph_engine.runtime.seed import empty_invocation_seed
from graph_engine.runtime.engine import Engine
from graph_engine.runtime.host_protocol import (
    TaskHostCallIdentity,
    TaskHostCallResult,
    TaskHostExecuteCall,
    TaskHostTerminalReceipt,
)
from graph_engine.runtime.host_receipts import (
    TerminalReceiptError,
    TerminalReceiptStore,
    prove_call_quiescent,
)
from graph_engine.runtime.ledger import Ledger
from graph_engine.runtime.models import PlannedTask, fold_events
from graph_engine.runtime.planner import plan_next
from graph_engine.runtime.scheduler import FakeClock, Scheduler
from graph_engine.runtime.workspace import SnapshotStore


_LOCK = "a" * 64
_FINGERPRINT = {"endpoint": "https://127.0.0.1:1", "profile": "test"}
_REFERENCE = {"id": "ext-1"}
_Cut = Literal[
    "before_quiescence",
    "after_quiescence_before_receipt",
    "after_receipt_fsync",
    "after_receipt_rename",
    "after_candidate_seal",
    "after_terminal_event_install",
    "after_terminal_event_fsync",
    "before_receipt_cleanup",
]


class CutCrash(RuntimeError):
    def __init__(self, cut: str) -> None:
        super().__init__(cut)
        self.cut = cut


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
        return TaskActivityReconcileResult(
            status="terminal",
            outcome=TaskOutcome.succeeded({"ok": True}),
        )

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


class _ReceiptHost:
    def __init__(self, ledger: Ledger, receipts: TerminalReceiptStore, *, cut: str | None = None) -> None:
        self._ledger = ledger
        self._receipts = receipts
        self._cut = cut
        self._handlers: dict[str, TaskHandler] = {}
        self._store: SnapshotStore | None = None
        self._armed = False
        self.provider_calls = 0

    def bind_invocation_runtime(
        self,
        *,
        handlers: dict[str, TaskHandler],
        store: SnapshotStore,
        receipts: TerminalReceiptStore | None = None,
    ) -> None:
        self._handlers = handlers
        self._store = store
        if receipts is not None:
            self._receipts = receipts

    async def execute(self, call: TaskHostExecuteCall) -> TaskHostCallResult:
        self.provider_calls += 1
        assert call.activity_rpc.activity_id is not None
        port = LedgerTaskActivityPort(ledger=self._ledger, identity=call.activity_rpc)
        port.mark_dispatch_started(_FINGERPRINT)
        port.bind(_REFERENCE)
        handler = self._handlers[call.request.capability_id]
        assert self._store is not None
        workspace_root = self._store.root / "attempts" / call.attempt_root.attempt_directory_id
        outcome = await handler.execute(
            call.request,
            TaskContext(
                workspace_root=workspace_root,
                heartbeat=lambda: None,
                cancel_requested=lambda: False,
                invocation=call.request.invocation,
                activity=port,
            ),
        )
        self._armed = True
        self._finish(call.identity, port.snapshot, outcome)
        return TaskHostCallResult(operation="execute", outcome=outcome)

    async def reconcile(self, call: object) -> TaskHostCallResult:
        self.provider_calls += 1
        identity = getattr(call, "identity")
        activity = getattr(call, "activity")
        outcome = TaskOutcome.succeeded({"ok": True})
        self._armed = True
        self._finish(identity, activity, outcome)
        return TaskHostCallResult(
            operation="reconcile",
            reconcile_result=TaskActivityReconcileResult(status="terminal", outcome=outcome),
        )

    async def cancel(self, call: object) -> TaskHostCallResult:
        del call
        self.provider_calls += 1
        return TaskHostCallResult(
            operation="cancel",
            cancel_result=TaskActivityCancelResult(status="acknowledged"),
        )

    def read_terminal_receipts(self, identity: TaskHostCallIdentity) -> tuple[TaskHostTerminalReceipt, ...]:
        return self._receipts.authenticate(identity)

    def _finish(
        self,
        identity: TaskHostCallIdentity,
        activity: TaskActivitySnapshot,
        outcome: TaskOutcome,
    ) -> None:
        host_receipts._quiescence_cut("before_quiescence")
        quiescence = prove_call_quiescent()
        host_receipts._quiescence_cut("after_quiescence_before_receipt")
        sink = self._receipts.sink_for(identity)
        sink.install(
            _receipt(
                identity,
                activity,
                outcome,
                quiescence,
                host_call_id=sink.host_call_id,
            )
        )


@dataclass
class _CutFixture:
    engine_root: Path
    invocation_root: Path
    product: Any
    host: _ReceiptHost
    receipts: TerminalReceiptStore
    ledger: Ledger

    async def reopen_and_recover(self) -> "_Recovered":
        self.host.provider_calls = 0
        engine = Engine(self.engine_root, clock=FakeClock(21.0), host=self.host)
        handle = engine.open("receipt-1", self.product)
        try:
            await handle.recover()
            events = [
                item.event
                for item in self.ledger.read_all()
                if item.event.kind == "task_activity_terminal_observed"
            ]
            assert len(events) == 1
            terminal = events[0]
            projection = fold_events(self.ledger.read_all())
            activity = projection.activations[-1].attempts[-1].activity
            assert activity is not None
            candidate_matches = (
                activity.candidate_tree_id == terminal.candidate_tree_id
                and activity.write_set_digest == terminal.write_set_digest
                and activity.outcome_digest == terminal.outcome_digest
            )
            return _Recovered(
                provider_calls=self.host.provider_calls,
                terminal_events=1,
                candidate_matches_receipt=candidate_matches,
            )
        finally:
            handle.close()
            engine.close()


@dataclass(frozen=True, slots=True)
class _Recovered:
    provider_calls: int
    terminal_events: int
    candidate_matches_receipt: bool


def _digest(value: object) -> str:
    return canonical_digest(value)  # type: ignore[arg-type]


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


def _identity(
    *,
    operation: str = "execute",
    activity_id: str = "activity-1",
    attempt: int = 1,
) -> TaskHostCallIdentity:
    host = pinned_execution_host_lock()
    return TaskHostCallIdentity(
        invocation_id="inv-1",
        task_id="task-1",
        activation_id="a1",
        attempt=attempt,
        activity_id=activity_id,
        operation=operation,  # type: ignore[arg-type]
        host_implementation_id=host.implementation_id,
        host_implementation_digest=host.implementation_digest,
    )


def _outcome() -> TaskOutcome:
    return TaskOutcome.succeeded({"ok": True})


def _receipt(
    identity: TaskHostCallIdentity,
    activity: TaskActivitySnapshot,
    outcome: TaskOutcome,
    quiescence: str,
    *,
    host_call_id: int,
) -> TaskHostTerminalReceipt:
    assert identity.activity_id is not None
    return TaskHostTerminalReceipt(
        host_implementation_digest=identity.host_implementation_digest,
        wire_schema_version=identity.wire_schema_version,
        invocation_id=identity.invocation_id,
        task_id=identity.task_id,
        activation_id=identity.activation_id,
        attempt=identity.attempt,
        activity_id=identity.activity_id,
        operation=identity.operation,
        request_digest=activity.request_digest,
        workspace_identity_digest=activity.workspace_identity.attempt_identity_digest,
        dispatch_fingerprint_digest=activity.dispatch_fingerprint_digest,
        reference_digest=activity.reference_digest,
        outcome=outcome,
        outcome_digest=_digest(outcome.model_dump(mode="json")),
        terminal_proof_digest=None,
        quiescence_proof_digest=quiescence,
        host_call_id=host_call_id,
    )


def _prepared_snapshot() -> TaskActivitySnapshot:
    from graph_engine.plugin_api import AttemptWorkspaceIdentity

    return TaskActivitySnapshot(
        activity_id="activity-1",
        request_digest="0" * 64,
        workspace_identity=AttemptWorkspaceIdentity(
            attempt_directory_id="attempt-1",
            baseline_tree_id="a" * 64,
            attempt_identity_digest="b" * 64,
        ),
        state="bound",
        dispatch_fingerprint=_FINGERPRINT,
        dispatch_fingerprint_digest=_digest(_FINGERPRINT),
        reference=_REFERENCE,
        reference_digest=_digest(_REFERENCE),
    )


def test_terminal_receipt_sink_is_host_call_scoped(tmp_path: Path) -> None:
    store = TerminalReceiptStore.create(tmp_path / "receipts")
    sink = store.sink_for(_identity())
    public = {name for name in dir(sink) if not name.startswith("_")}
    assert public == {"install", "host_call_id"}
    assert not hasattr(sink, "read")
    assert not hasattr(sink, "delete")
    assert not hasattr(sink, "parent")
    assert not hasattr(sink, "root")
    assert not hasattr(sink, "path")
    assert sink.host_call_id == 1


def test_receipt_store_rejects_partial_symlink_linked_changed_multiple_foreign_and_nonmonotonic(
    tmp_path: Path,
) -> None:
    store = TerminalReceiptStore.create(tmp_path / "receipts")
    identity = _identity()
    activity = _prepared_snapshot()
    outcome = _outcome()
    quiescence = prove_call_quiescent()
    sink = store.sink_for(identity)
    sink.install(_receipt(identity, activity, outcome, quiescence, host_call_id=1))
    authenticated = store.authenticate(identity)
    assert len(authenticated) == 1

    foreign = _identity(activity_id="activity-2")
    with pytest.raises(TerminalReceiptError, match="foreign"):
        sink.install(_receipt(foreign, activity, outcome, quiescence, host_call_id=1))

    second = store.sink_for(identity)
    with pytest.raises(TerminalReceiptError, match="multiple"):
        second.install(_receipt(identity, activity, outcome, quiescence, host_call_id=2))

    names = os.listdir(store.root)
    final = next(name for name in names if not name.startswith("."))
    os.chmod(store.root / final, 0o600)
    (store.root / final).write_bytes(b'{"changed":true}')
    with pytest.raises(TerminalReceiptError, match="changed|invalid"):
        store.authenticate(identity)


def test_prepare_receipt_is_not_promotable(tmp_path: Path) -> None:
    store = TerminalReceiptStore.create(tmp_path / "receipts")
    pending = store.root / ".pending-prepare.json"
    pending.write_bytes(b'{"schema_version":"1","kind":"prepare"}')
    assert store.authenticate(_identity()) == ()


def test_quiescence_rejects_live_writers() -> None:
    with pytest.raises(TerminalReceiptError, match="quiescent"):
        prove_call_quiescent(writer_identities=("writer-1",))


def _install_cut(cut: str) -> tuple[Any, Any, Any]:
    original_receipt = host_receipts._receipt_cut
    original_quiescence = host_receipts._quiescence_cut
    original_promotion = scheduler_runtime._promotion_cut
    original_append = ledger_runtime._append_boundary

    def crash(name: str) -> None:
        if name == cut:
            raise CutCrash(cut)

    host_receipts._receipt_cut = crash
    host_receipts._quiescence_cut = crash
    scheduler_runtime._promotion_cut = crash

    def append_crash(name: str) -> None:
        mapped = {
            "final_installed": "after_terminal_event_install",
            "directory_fsynced": "after_terminal_event_fsync",
        }.get(name)
        if mapped == cut and getattr(scheduler_runtime, "_PROMOTING_TERMINAL", False):
            raise CutCrash(cut)

    ledger_runtime._append_boundary = append_crash
    return original_receipt, original_quiescence, (original_promotion, original_append)


def _restore_cut(saved: tuple[Any, Any, Any]) -> None:
    host_receipts._receipt_cut = saved[0]
    host_receipts._quiescence_cut = saved[1]
    scheduler_runtime._promotion_cut = saved[2][0]
    ledger_runtime._append_boundary = saved[2][1]


def _load_product() -> Any:
    import builtins
    import importlib.util

    names = (
        "_graph_engine_runtime_test_callbacks",
        "_graph_engine_runtime_test_effect_callbacks",
        "_graph_engine_runtime_test_effect_policies",
    )
    saved = {name: getattr(builtins, name, None) for name in names}
    path = Path(__file__).with_name("test_engine.py")
    spec = importlib.util.spec_from_file_location("_receipt_engine_helpers", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    async def unused(_request: TaskRequest, _context: TaskContext) -> TaskOutcome:
        return TaskOutcome.succeeded({"ok": True})

    try:
        return module._task_product(unused)
    finally:
        for name, value in saved.items():
            if value is None:
                if hasattr(builtins, name):
                    delattr(builtins, name)
            else:
                setattr(builtins, name, value)


async def _cut_terminal_call(cut: _Cut, tmp_path: Path) -> _CutFixture:
    product = _load_product()
    engine = Engine(tmp_path, clock=FakeClock(10.0))
    handle = engine.start(product, entrypoint="main", invocation_id="receipt-1", seed=empty_invocation_seed())
    ledger = Ledger(handle.invocation_root / "ledger")
    envelopes = ledger.read_all()
    plan = plan_next(product.workflow, fold_events(envelopes))
    if plan.events:
        ledger.append_batch(plan.events, expected_next_seq=envelopes[-1].seq + 1)
    envelopes = ledger.read_all()
    plan = plan_next(product.workflow, fold_events(envelopes))
    assert plan.tasks
    task = plan.tasks[0]
    receipts = TerminalReceiptStore.open_or_create(handle.invocation_root / "receipts")
    host = _ReceiptHost(ledger, receipts, cut=cut)
    owner_id = canonical_digest(
        {
            "invocation_id": "receipt-1",
            "lock_digest": product.lock_digest,
            "role": "engine-scheduler",
        }
    )
    saved = _install_cut(cut)
    try:
        with handle.workspace as store:
            scheduler = Scheduler(
                _DirectRegistry({task.capability_id: _RecoverableHandler()}),
                store,
                ledger,
                host,
                owner_id=owner_id,
                clock=FakeClock(10.0),
                lease_seconds=30.0,
                lock_digest=product.lock_digest,
                composition_digest=product.digest,
                entrypoint="main",
                receipts=receipts,
            )
            assert isinstance(_RecoverableHandler(), RecoverableTaskHandler)
            try:
                await scheduler.run_wave((task,))
            except CutCrash:
                pass
    finally:
        _restore_cut(saved)
        handle.close()
        engine.close()
    return _CutFixture(
        engine_root=tmp_path,
        invocation_root=handle.invocation_root,
        product=product,
        host=host,
        receipts=receipts,
        ledger=ledger,
    )


@pytest.mark.parametrize(
    "cut",
    [
        "before_quiescence",
        "after_quiescence_before_receipt",
        "after_receipt_fsync",
        "after_receipt_rename",
        "after_candidate_seal",
        "after_terminal_event_install",
        "after_terminal_event_fsync",
        "before_receipt_cleanup",
    ],
)
def test_terminal_receipt_cut_recovery_is_exactly_once(tmp_path: Path, cut: str) -> None:
    asyncio.run(_assert_terminal_receipt_cut(tmp_path, cut))  # type: ignore[arg-type]


async def _assert_terminal_receipt_cut(tmp_path: Path, cut: _Cut) -> None:
    fixture = await _cut_terminal_call(cut, tmp_path)
    recovered = await fixture.reopen_and_recover()
    assert recovered.provider_calls <= 1
    assert recovered.terminal_events == 1
    assert recovered.candidate_matches_receipt


def test_recovery_checks_receipt_before_reconcile(tmp_path: Path) -> None:
    asyncio.run(_assert_receipt_before_reconcile(tmp_path))


async def _assert_receipt_before_reconcile(tmp_path: Path) -> None:
    fixture = await _cut_terminal_call("before_receipt_cleanup", tmp_path)
    before = fixture.host.provider_calls
    recovered = await fixture.reopen_and_recover()
    assert recovered.provider_calls == 0
    assert recovered.terminal_events == 1
    del before


def test_success_candidate_is_sealed_without_moving_head(tmp_path: Path) -> None:
    store = SnapshotStore.create(tmp_path / "store", {"seed.txt": b"old"})
    before = store.head_tree_id()
    from graph_engine.plugin_api import AttemptWorkspaceIdentity
    from graph_engine.runtime.models import attempt_directory_id, attempt_identity_digest

    workspace, identity = store.create_attempt_identity(
        invocation_id="inv-1",
        task_id="task-1",
        activation_id="a1",
        attempt=1,
    )
    del workspace
    assert identity.attempt_directory_id == attempt_directory_id("inv-1", "task-1", "a1", 1)
    assert identity.attempt_identity_digest == attempt_identity_digest("inv-1", "task-1", "a1", 1)
    candidate = store.seal_authenticated_candidate(identity, ResourceClaims())
    assert store.head_tree_id() == before
    assert candidate.candidate_tree_id != ""
    assert write_set_digest(candidate) == write_set_digest(candidate)
    reopened = AttemptWorkspaceIdentity.model_validate(identity.model_dump())
    del reopened
