from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from bootstrap_fixtures import synthetic_invocation_started
from graph_engine.canonical import canonical_digest
from graph_engine.composition.lock import pinned_execution_host_lock
from graph_engine.plugin_api import (
    AttemptWorkspaceIdentity,
    InvocationMetadata,
    ResourceClaims,
    TaskActivityCancelResult,
    TaskActivityReconcileResult,
    TaskActivitySnapshot,
    TaskContext,
    TaskOutcome,
    TaskRequest,
)
from graph_engine.runtime.host_protocol import (
    AttemptRootDescriptor,
    TaskActivityRpcIdentity,
    TaskHostCallIdentity,
    TaskHostCancelCall,
    TaskHostExecuteCall,
    TaskHostReconcileCall,
    TaskHostTerminalReceipt,
)
from graph_engine.runtime.events import (
    GraphStarted,
    NodeActivated,
    TaskActivityPrepared,
    TaskAttemptStarted,
    TaskLeaseAcquired,
    TokenConsumed,
    TokenOffered,
)
from graph_engine.runtime.host_receipts import (
    TerminalReceiptError,
    TerminalReceiptStore,
    prove_call_quiescent,
)
from graph_engine.runtime.ledger import Ledger
from graph_engine.runtime.production_host import ProductionHostError, _ProductionTaskExecutionHost
from graph_engine.runtime.secret_sources import empty_runtime_authorization
from graph_engine.runtime.workspace import SnapshotStore


def _write_handler(tmp_path: Path, *, class_name: str, body: str) -> tuple[str, tuple[str, ...]]:
    module_path = tmp_path / f"{class_name.lower()}.py"
    module_path.write_text(
        "from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest\n\n"
        f"class {class_name}:\n"
        f"{textwrap.indent(body, '    ')}\n",
        encoding="utf-8",
    )
    return f"{module_path.stem}:{class_name}.execute", (str(tmp_path),)


class _EchoHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del request
        (context.workspace_root / "done.txt").write_text("ok\n", encoding="utf-8")
        return TaskOutcome.succeeded({"ok": True})


class _RecoverableEcho:
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
        return TaskActivityReconcileResult(status="not_dispatched")

    async def cancel(
        self,
        request: TaskRequest,
        context: TaskContext,
        activity: TaskActivitySnapshot,
    ) -> TaskActivityCancelResult:
        del request, context, activity
        return TaskActivityCancelResult(status="acknowledged")


def _request() -> TaskRequest:
    return TaskRequest(
        invocation_id="inv-1",
        task_id="task-1",
        graph_instance_id="graph-1",
        node_id="run",
        capability_id="test.echo.run",
        target_capability_id="test.echo.run",
        binding_data={},
        resource_ids=(),
        resource_digests={},
        resources=ResourceClaims(),
        invocation=InvocationMetadata(
            invocation_id="inv-1",
            lock_digest="a" * 64,
            composition_digest=canonical_digest({"lock_digest": "a" * 64}),
            entrypoint="main",
        ),
        attempt=1,
        input={},
    )


def _execute_call(
    *,
    attempt_directory_id: str = "attempt-1",
    capability_id: str = "test.echo.run",
    entrypoint: str = "echo_handler:EchoHandler.execute",
    activity_id: str | None = None,
    timeout_seconds: float = 30.0,
) -> TaskHostExecuteCall:
    host = pinned_execution_host_lock()
    return TaskHostExecuteCall(
        identity=TaskHostCallIdentity(
            invocation_id="inv-1",
            task_id="task-1",
            activation_id="activation-run",
            attempt=1,
            activity_id=activity_id,
            operation="execute",
            host_implementation_id=host.implementation_id,
            host_implementation_digest=host.implementation_digest,
        ),
        capability_id=capability_id,
        capability_entrypoint=entrypoint,
        request=_request().model_copy(
            update={"capability_id": capability_id, "target_capability_id": capability_id}
        ),
        attempt_root=AttemptRootDescriptor(attempt_directory_id=attempt_directory_id),
        activity_rpc=TaskActivityRpcIdentity(
            invocation_id="inv-1",
            task_id="task-1",
            activation_id="activation-run",
            attempt=1,
            activity_id=activity_id,
        ),
        authorized_secret_handles=(),
        timeout_seconds=timeout_seconds,
    )


def _activity_snapshot() -> TaskActivitySnapshot:
    fingerprint = {"endpoint": "https://example.test"}
    workspace = AttemptWorkspaceIdentity(
        attempt_directory_id="attempt-1",
        baseline_tree_id="0" * 64,
        attempt_identity_digest="1" * 64,
    )
    return TaskActivitySnapshot(
        activity_id="activity-1",
        request_digest="2" * 64,
        workspace_identity=workspace,
        dispatch_fingerprint=fingerprint,
        dispatch_fingerprint_digest=canonical_digest(fingerprint),
        state="dispatch_started",
    )


def _reconcile_call(*, entrypoint: str) -> TaskHostReconcileCall:
    host = pinned_execution_host_lock()
    identity = TaskHostCallIdentity(
        invocation_id="inv-1",
        task_id="task-1",
        activation_id="activation-run",
        attempt=1,
        activity_id="activity-1",
        operation="reconcile",
        host_implementation_id=host.implementation_id,
        host_implementation_digest=host.implementation_digest,
    )
    return TaskHostReconcileCall(
        identity=identity,
        capability_id="test.echo.run",
        capability_entrypoint=entrypoint,
        request=_request(),
        attempt_root=AttemptRootDescriptor(attempt_directory_id="attempt-1"),
        activity_rpc=TaskActivityRpcIdentity(
            invocation_id="inv-1",
            task_id="task-1",
            activation_id="activation-run",
            attempt=1,
            activity_id="activity-1",
        ),
        authorized_secret_handles=(),
        activity=_activity_snapshot(),
    )


def _cancel_call(*, entrypoint: str) -> TaskHostCancelCall:
    host = pinned_execution_host_lock()
    identity = TaskHostCallIdentity(
        invocation_id="inv-1",
        task_id="task-1",
        activation_id="activation-run",
        attempt=1,
        activity_id="activity-1",
        operation="cancel",
        host_implementation_id=host.implementation_id,
        host_implementation_digest=host.implementation_digest,
    )
    return TaskHostCancelCall(
        identity=identity,
        capability_id="test.echo.run",
        capability_entrypoint=entrypoint,
        request=_request(),
        attempt_root=AttemptRootDescriptor(attempt_directory_id="attempt-1"),
        activity_rpc=TaskActivityRpcIdentity(
            invocation_id="inv-1",
            task_id="task-1",
            activation_id="activation-run",
            attempt=1,
            activity_id="activity-1",
        ),
        authorized_secret_handles=(),
        activity=_activity_snapshot(),
    )


def test_production_host_rejects_wrong_workspace(tmp_path: Path) -> None:
    store = SnapshotStore.create(tmp_path / "workspace", {})
    store.create_attempt("attempt-1")
    entrypoint, roots = _write_handler(
        tmp_path,
        class_name="EchoHandler",
        body="async def execute(self, request, context):\n    return TaskOutcome.succeeded({'ok': True})",
    )
    host = _ProductionTaskExecutionHost(root=tmp_path, authorization=empty_runtime_authorization())
    host.bind_invocation_runtime(
        handlers={"test.echo.run": _EchoHandler()},
        store=store,
        handler_import_roots={"test.echo.run": roots},
    )
    call = _execute_call(attempt_directory_id="missing-attempt", entrypoint=entrypoint)
    with pytest.raises(ProductionHostError, match="attempt workspace"):
        asyncio.run(host.execute(call))


def test_unrelated_workspace_holder_does_not_block_quiescence(tmp_path: Path) -> None:
    store = SnapshotStore.create(tmp_path / "workspace", {})
    attempt = store.create_attempt("attempt-1")
    held = attempt.root / "held-by-provider.txt"
    held.write_text("open\n", encoding="utf-8")
    holder = subprocess.Popen(
        [sys.executable, "-c", "import time; open(r'''" + str(held) + "'''); time.sleep(60)"],
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and holder.poll() is not None:
            time.sleep(0.05)
        assert holder.poll() is None
        entrypoint, roots = _write_handler(
            tmp_path,
            class_name="EchoHandler",
            body="async def execute(self, request, context):\n    return TaskOutcome.succeeded({'ok': True})",
        )
        host = _ProductionTaskExecutionHost(root=tmp_path, authorization=empty_runtime_authorization())
        host.bind_invocation_runtime(
            handlers={"test.echo.run": _EchoHandler()},
            store=store,
            handler_import_roots={"test.echo.run": roots},
        )
        result = asyncio.run(host.execute(_execute_call(entrypoint=entrypoint)))
        assert result.outcome is not None
        assert result.outcome.status == "succeeded"
    finally:
        holder.kill()
        holder.wait(timeout=2)


def test_leftover_process_group_child_still_fails_quiescence(tmp_path: Path) -> None:
    store = SnapshotStore.create(tmp_path / "workspace", {})
    store.create_attempt("attempt-1")
    entrypoint, roots = _write_handler(
        tmp_path,
        class_name="OrphanHandler",
        body=(
            "async def execute(self, request, context):\n"
            "    import subprocess, sys, time\n"
            "    held = context.workspace_root / 'held.txt'\n"
            "    held.write_text('open\\n', encoding='utf-8')\n"
            "    child = subprocess.Popen(\n"
            "        [sys.executable, '-c', 'import time; time.sleep(60)'],\n"
            "    )\n"
            "    (context.workspace_root / 'orphan.pid').write_text(str(child.pid), encoding='utf-8')\n"
            "    time.sleep(0.2)\n"
            "    return TaskOutcome.succeeded({'ok': True})\n"
        ),
    )
    host = _ProductionTaskExecutionHost(root=tmp_path, authorization=empty_runtime_authorization())
    host.bind_invocation_runtime(
        handlers={"test.echo.run": _EchoHandler()},
        store=store,
        handler_import_roots={"test.echo.run": roots},
    )
    try:
        with pytest.raises(TerminalReceiptError, match="not quiescent"):
            asyncio.run(host.execute(_execute_call(entrypoint=entrypoint)))
    finally:
        pid_path = tmp_path / "workspace" / "attempts" / "attempt-1" / "orphan.pid"
        if pid_path.is_file():
            try:
                os.kill(int(pid_path.read_text(encoding="utf-8")), 9)
            except OSError:
                pass


def test_production_host_rejects_forged_terminal_receipt(tmp_path: Path) -> None:
    store = TerminalReceiptStore.create(tmp_path / "receipts")
    identity = TaskHostCallIdentity(
        invocation_id="inv-1",
        task_id="task-1",
        activation_id="activation-run",
        attempt=1,
        activity_id="activity-1",
        operation="execute",
        host_implementation_id="graph.engine.task-host",
        host_implementation_digest="f" * 64,
    )
    sink = store.sink_for(identity)
    outcome = TaskOutcome.succeeded({"ok": True})
    receipt = TaskHostTerminalReceipt(
        host_implementation_digest=identity.host_implementation_digest,
        invocation_id="inv-2",
        task_id=identity.task_id,
        activation_id=identity.activation_id,
        attempt=identity.attempt,
        activity_id=identity.activity_id,
        operation=identity.operation,
        request_digest="0" * 64,
        workspace_identity_digest="1" * 64,
        outcome=outcome,
        outcome_digest=canonical_digest(outcome.model_dump(mode="json")),
        quiescence_proof_digest=prove_call_quiescent(),
        host_call_id=sink.host_call_id,
    )
    with pytest.raises(TerminalReceiptError, match="foreign terminal receipt"):
        sink.install(receipt)


def test_production_host_worker_crash_before_response(tmp_path: Path) -> None:
    store = SnapshotStore.create(tmp_path / "workspace", {})
    store.create_attempt("attempt-1")
    entrypoint, roots = _write_handler(
        tmp_path,
        class_name="MissingHandler",
        body="async def execute(self, request, context):\n    raise RuntimeError('worker crash')",
    )
    host = _ProductionTaskExecutionHost(root=tmp_path, authorization=empty_runtime_authorization())
    host.bind_invocation_runtime(
        handlers={"test.echo.run": _EchoHandler()},
        store=store,
        handler_import_roots={"test.echo.run": roots},
    )
    with pytest.raises(ProductionHostError, match="worker (exited|control stream closed)"):
        asyncio.run(host.execute(_execute_call(entrypoint=entrypoint)))


def test_production_host_reconcile_from_installed_receipt(tmp_path: Path) -> None:
    store = SnapshotStore.create(tmp_path / "workspace", {})
    store.create_attempt("attempt-1")
    receipts = TerminalReceiptStore.create(tmp_path / "receipts")
    host = _ProductionTaskExecutionHost(root=tmp_path, authorization=empty_runtime_authorization())
    host.bind_invocation_runtime(
        handlers={"test.echo.run": _RecoverableEcho()},
        store=store,
        receipts=receipts,
    )
    reconcile = _reconcile_call(entrypoint="recoverable:RecoverableEcho.reconcile")
    outcome = TaskOutcome.failed("transient", "already finished")
    sink = receipts.sink_for(reconcile.identity.model_copy(update={"operation": "execute"}))
    sink.install(
        TaskHostTerminalReceipt(
            host_implementation_digest=reconcile.identity.host_implementation_digest,
            wire_schema_version=reconcile.identity.wire_schema_version,
            invocation_id=reconcile.identity.invocation_id,
            task_id=reconcile.identity.task_id,
            activation_id=reconcile.identity.activation_id,
            attempt=reconcile.identity.attempt,
            activity_id=reconcile.identity.activity_id,
            operation="execute",
            request_digest=reconcile.activity.request_digest,
            workspace_identity_digest=reconcile.activity.workspace_identity.attempt_identity_digest,
            dispatch_fingerprint_digest=reconcile.activity.dispatch_fingerprint_digest,
            reference_digest=reconcile.activity.reference_digest,
            outcome=outcome,
            outcome_digest=canonical_digest(outcome.model_dump(mode="json")),
            terminal_proof_digest=None,
            quiescence_proof_digest=prove_call_quiescent(),
            host_call_id=sink.host_call_id,
        )
    )
    result = asyncio.run(host.reconcile(reconcile))
    assert result.reconcile_result is not None
    assert result.reconcile_result.status == "terminal"
    assert result.reconcile_result.outcome == outcome


def test_production_host_cancel_runs_through_worker(tmp_path: Path) -> None:
    store = SnapshotStore.create(tmp_path / "workspace", {})
    store.create_attempt("attempt-1")
    entrypoint, roots = _write_handler(
        tmp_path,
        class_name="RecoverableEcho",
        body=(
            "async def cancel(self, request, context, activity):\n"
            "    from graph_engine.plugin_api import TaskActivityCancelResult\n"
            "    return TaskActivityCancelResult(status='acknowledged')"
        ),
    )
    cancel_entrypoint = entrypoint.replace(".execute", ".cancel")
    host = _ProductionTaskExecutionHost(root=tmp_path, authorization=empty_runtime_authorization())
    host.bind_invocation_runtime(
        handlers={"test.echo.run": _RecoverableEcho()},
        store=store,
        handler_import_roots={"test.echo.run": roots},
    )
    result = asyncio.run(host.cancel(_cancel_call(entrypoint=cancel_entrypoint)))
    assert result.cancel_result is not None
    assert result.cancel_result.status == "acknowledged"


def test_production_host_parent_alive_pipe_is_wired(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from graph_engine.runtime import production_host as module

    captured: dict[str, int] = {}

    original_spawn = module._ProcessSupervisor.spawn

    def recording_spawn(self: module._ProcessSupervisor, *, attempt_root: Path, call_digest: str) -> object:
        worker = original_spawn(self, attempt_root=attempt_root, call_digest=call_digest)
        captured["parent_alive_w"] = worker.parent_alive_w
        return worker

    monkeypatch.setattr(module._ProcessSupervisor, "spawn", recording_spawn)
    store = SnapshotStore.create(tmp_path / "workspace", {})
    store.create_attempt("attempt-1")
    entrypoint, roots = _write_handler(
        tmp_path,
        class_name="EchoHandler",
        body="async def execute(self, request, context):\n    return TaskOutcome.succeeded({'ok': True})",
    )
    host = _ProductionTaskExecutionHost(root=tmp_path, authorization=empty_runtime_authorization())
    host.bind_invocation_runtime(
        handlers={"test.echo.run": _EchoHandler()},
        store=store,
        handler_import_roots={"test.echo.run": roots},
    )
    asyncio.run(host.execute(_execute_call(entrypoint=entrypoint)))
    assert captured["parent_alive_w"] >= 0


def test_production_host_cancel_escalates_on_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from graph_engine.runtime import production_host as module

    monkeypatch.setattr(module, "_CALL_TIMEOUT_SECONDS", 0.2)
    store = SnapshotStore.create(tmp_path / "workspace", {})
    store.create_attempt("attempt-1")
    entrypoint, roots = _write_handler(
        tmp_path,
        class_name="SlowHandler",
        body=(
            "async def execute(self, request, context):\n"
            "    import asyncio\n"
            "    while True:\n"
            "        await asyncio.sleep(0.05)\n"
        ),
    )
    host = _ProductionTaskExecutionHost(root=tmp_path, authorization=empty_runtime_authorization())
    host.bind_invocation_runtime(
        handlers={"test.echo.run": _EchoHandler()},
        store=store,
        handler_import_roots={"test.echo.run": roots},
    )
    with pytest.raises(ProductionHostError, match="timed out"):
        asyncio.run(host.execute(_execute_call(entrypoint=entrypoint, timeout_seconds=0.2)))


def test_production_host_honors_call_timeout_longer_than_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from graph_engine.runtime import production_host as module

    monkeypatch.setattr(module, "_CALL_TIMEOUT_SECONDS", 0.2)
    store = SnapshotStore.create(tmp_path / "workspace", {})
    store.create_attempt("attempt-1")
    entrypoint, roots = _write_handler(
        tmp_path,
        class_name="PauseHandler",
        body=(
            "async def execute(self, request, context):\n"
            "    import asyncio\n"
            "    await asyncio.sleep(0.4)\n"
            "    return TaskOutcome.succeeded({'ok': True})\n"
        ),
    )
    host = _ProductionTaskExecutionHost(root=tmp_path, authorization=empty_runtime_authorization())
    host.bind_invocation_runtime(
        handlers={"test.echo.run": _EchoHandler()},
        store=store,
        handler_import_roots={"test.echo.run": roots},
    )
    result = asyncio.run(host.execute(_execute_call(entrypoint=entrypoint, timeout_seconds=2.0)))
    assert result.outcome is not None
    assert result.outcome.status == "succeeded"


def test_production_host_crash_after_receipt_leaves_durable_receipt(tmp_path: Path) -> None:
    store = SnapshotStore.create(tmp_path / "workspace", {})
    store.create_attempt("attempt-1")
    receipts = TerminalReceiptStore.create(tmp_path / "receipts")
    host = _ProductionTaskExecutionHost(root=tmp_path, authorization=empty_runtime_authorization())
    host.bind_invocation_runtime(
        handlers={"test.echo.run": _RecoverableEcho()},
        store=store,
        receipts=receipts,
    )
    reconcile = _reconcile_call(entrypoint="recoverable:RecoverableEcho.reconcile")
    outcome = TaskOutcome.failed("transient", "already finished")
    sink = receipts.sink_for(reconcile.identity.model_copy(update={"operation": "execute"}))
    sink.install(
        TaskHostTerminalReceipt(
            host_implementation_digest=reconcile.identity.host_implementation_digest,
            wire_schema_version=reconcile.identity.wire_schema_version,
            invocation_id=reconcile.identity.invocation_id,
            task_id=reconcile.identity.task_id,
            activation_id=reconcile.identity.activation_id,
            attempt=reconcile.identity.attempt,
            activity_id=reconcile.identity.activity_id,
            operation="execute",
            request_digest=reconcile.activity.request_digest,
            workspace_identity_digest=reconcile.activity.workspace_identity.attempt_identity_digest,
            dispatch_fingerprint_digest=reconcile.activity.dispatch_fingerprint_digest,
            reference_digest=reconcile.activity.reference_digest,
            outcome=outcome,
            outcome_digest=canonical_digest(outcome.model_dump(mode="json")),
            terminal_proof_digest=None,
            quiescence_proof_digest=prove_call_quiescent(),
            host_call_id=sink.host_call_id,
        )
    )
    result = asyncio.run(host.reconcile(reconcile))
    assert result.reconcile_result is not None
    promoted = receipts.authenticate(reconcile.identity.model_copy(update={"operation": "execute"}))
    assert len(promoted) == 1
    assert promoted[0].outcome == outcome


def _prepare_activity_ledger(root: Path) -> None:
    workspace = AttemptWorkspaceIdentity(
        attempt_directory_id="attempt-1",
        baseline_tree_id="0" * 64,
        attempt_identity_digest="1" * 64,
    )
    ledger = Ledger(root / "invocations" / "inv-1" / "ledger")
    ledger.append_batch(
        (
            synthetic_invocation_started(),
            GraphStarted(graph_instance_id="root", graph_id="root"),
            TokenOffered(
                token_id="tok-1",
                graph_instance_id="root",
                source=None,
                target="run",
                payload=None,
            ),
            TokenConsumed(token_id="tok-1", graph_instance_id="root", node_id="run"),
            NodeActivated(
                activation_id="activation-run",
                graph_instance_id="root",
                node_id="run",
                token_ids=("tok-1",),
            ),
            TaskAttemptStarted(activation_id="activation-run", attempt=1, lease_expires_at="11"),
            TaskLeaseAcquired(
                task_id="task-1",
                activation_id="activation-run",
                attempt=1,
                owner_id="worker-1",
                acquired_at=1.0,
                heartbeat_at=1.0,
                expires_at=11.0,
            ),
            TaskActivityPrepared(
                activity_id="activity-1",
                task_id="task-1",
                activation_id="activation-run",
                attempt=1,
                request_digest="2" * 64,
                workspace_identity=workspace,
            ),
        ),
        expected_next_seq=1,
    )


def test_production_host_activity_snapshot_rpc_completes(tmp_path: Path) -> None:
    _prepare_activity_ledger(tmp_path)
    store = SnapshotStore.create(tmp_path / "workspace", {})
    store.create_attempt("attempt-1")
    entrypoint, roots = _write_handler(
        tmp_path,
        class_name="SnapshotHandler",
        body=(
            "async def execute(self, request, context):\n"
            "    if context.activity is None:\n"
            "        raise ValueError('activity port is required')\n"
            "    snapshot = context.activity.snapshot\n"
            "    (context.workspace_root / 'after.txt').write_text(snapshot.activity_id, encoding='utf-8')\n"
            "    return TaskOutcome.succeeded({'ok': True})\n"
        ),
    )
    host = _ProductionTaskExecutionHost(root=tmp_path, authorization=empty_runtime_authorization())
    host.bind_invocation_runtime(
        handlers={"test.echo.run": _EchoHandler()},
        store=store,
        handler_import_roots={"test.echo.run": roots},
    )
    result = asyncio.run(
        asyncio.wait_for(
            host.execute(_execute_call(entrypoint=entrypoint, activity_id="activity-1")),
            timeout=5.0,
        )
    )
    assert result.outcome is not None
    assert result.outcome.status == "succeeded"
    attempt = tmp_path / "workspace" / "attempts" / "attempt-1"
    assert (attempt / "after.txt").read_text(encoding="utf-8") == "activity-1"
