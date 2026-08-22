from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from graph_engine.canonical import canonical_digest
from graph_engine.composition.lock import pinned_execution_host_lock
from graph_engine.plugin_api import (
    AttemptWorkspaceIdentity,
    InvocationMetadata,
    ResourceClaims,
    TaskContext,
    TaskHandler,
    TaskOutcome,
    TaskRequest,
)
from graph_engine.runtime.host_protocol import (
    AttemptRootDescriptor,
    TaskActivityRpcIdentity,
    TaskHostCallIdentity,
    TaskHostExecuteCall,
    TaskHostTerminalReceipt,
)
from graph_engine.runtime.host_receipts import TerminalReceiptError, TerminalReceiptStore, prove_call_quiescent
from graph_engine.runtime.production_host import ProductionHostError, _ProductionTaskExecutionHost
from graph_engine.runtime.secret_sources import empty_runtime_authorization
from graph_engine.runtime.workspace import SnapshotStore


class _EchoHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del request
        (context.workspace_root / "done.txt").write_text("ok\n", encoding="utf-8")
        return TaskOutcome.succeeded({"ok": True})


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


def _execute_call(*, attempt_directory_id: str = "attempt-1") -> TaskHostExecuteCall:
    host = pinned_execution_host_lock()
    return TaskHostExecuteCall(
        identity=TaskHostCallIdentity(
            invocation_id="inv-1",
            task_id="task-1",
            activation_id="activation-run",
            attempt=1,
            activity_id=None,
            operation="execute",
            host_implementation_id=host.implementation_id,
            host_implementation_digest=host.implementation_digest,
        ),
        capability_id="test.echo.run",
        capability_entrypoint="__main__:missing",
        request=_request(),
        attempt_root=AttemptRootDescriptor(attempt_directory_id=attempt_directory_id),
        activity_rpc=TaskActivityRpcIdentity(
            invocation_id="inv-1",
            task_id="task-1",
            activation_id="activation-run",
            attempt=1,
            activity_id=None,
        ),
        authorized_secret_handles=(),
    )


def test_production_host_rejects_wrong_workspace(tmp_path: Path) -> None:
    store = SnapshotStore.create(tmp_path / "workspace", {})
    store.create_attempt("attempt-1")
    host = _ProductionTaskExecutionHost(root=tmp_path, authorization=empty_runtime_authorization())
    host.bind_invocation_runtime(handlers={"test.echo.run": _EchoHandler()}, store=store)
    call = _execute_call(attempt_directory_id="missing-attempt")
    with pytest.raises(ProductionHostError, match="attempt workspace"):
        asyncio.run(host.execute(call))


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


def test_production_host_worker_crash_before_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SnapshotStore.create(tmp_path / "workspace", {})
    store.create_attempt("attempt-1")
    host = _ProductionTaskExecutionHost(root=tmp_path, authorization=empty_runtime_authorization())
    host.bind_invocation_runtime(handlers={"test.echo.run": _EchoHandler()}, store=store)

    def crash_worker(*args: object, **kwargs: object) -> TaskHostCallResult:
        raise ProductionHostError("worker exited with status 1")

    monkeypatch.setattr(_ProductionTaskExecutionHost, "_drive_worker", crash_worker)
    with pytest.raises(ProductionHostError, match="worker exited"):
        asyncio.run(host.execute(_execute_call()))
