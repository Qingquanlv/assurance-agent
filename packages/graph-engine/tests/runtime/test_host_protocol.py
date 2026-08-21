from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

import pytest
from pydantic import ValidationError

from graph_engine.canonical import canonical_digest, canonical_json_bytes
from graph_engine.plugin_api import (
    AttemptWorkspaceIdentity,
    InvocationMetadata,
    ResourceClaims,
    SecretHandleUnauthorized,
    SecretPort,
    TaskActivityCancelResult,
    TaskActivityPort,
    TaskActivityReconcileResult,
    TaskActivitySnapshot,
    TaskContext,
    TaskOutcome,
    TaskRequest,
)
from graph_engine.runtime.host_protocol import (
    TASK_HOST_WIRE_SCHEMA_VERSION,
    AttemptRootDescriptor,
    TaskActivityRpcIdentity,
    TaskExecutionHost,
    TaskHostCancelCall,
    TaskHostCallIdentity,
    TaskHostCallResult,
    TaskHostExecuteCall,
    TaskHostReconcileCall,
    TaskHostTerminalReceipt,
    authorized_secret_port,
)


_LOCK_DIGEST = "a" * 64
_COMPOSITION_DIGEST = canonical_digest({"lock_digest": _LOCK_DIGEST})
_CANARY = b"canary-secret-value"


def _invocation() -> InvocationMetadata:
    return InvocationMetadata(
        invocation_id="inv-1",
        lock_digest=_LOCK_DIGEST,
        composition_digest=_COMPOSITION_DIGEST,
        entrypoint="main",
    )


def _request() -> TaskRequest:
    return TaskRequest(
        invocation_id="inv-1",
        task_id="task-1",
        graph_instance_id="graph-1",
        node_id="run",
        capability_id="fixture.agent.run",
        target_capability_id="runtime.opencode.execute",
        binding_data={"profile": "fixture-default"},
        resource_ids=("fixture.instructions", "fixture.result-schema"),
        resource_digests={
            "fixture.instructions": "d" * 64,
            "fixture.result-schema": "e" * 64,
        },
        resources=ResourceClaims(),
        invocation=_invocation(),
        attempt=1,
        input={"prompt": "go"},
    )


def _secret_port(authorized: dict[str, bytes]) -> SecretPort:
    return authorized_secret_port(authorized)


def _identity(*, operation: str = "execute") -> TaskHostCallIdentity:
    return TaskHostCallIdentity(
        invocation_id="inv-1",
        task_id="task-1",
        activation_id="activation-run",
        attempt=1,
        activity_id=None,
        operation=operation,  # type: ignore[arg-type]
        host_implementation_id="graph.engine.task-host",
        host_implementation_digest="f" * 64,
        wire_schema_version=TASK_HOST_WIRE_SCHEMA_VERSION,
    )


def _execute_call() -> TaskHostExecuteCall:
    return TaskHostExecuteCall(
        identity=_identity(),
        capability_id="runtime.opencode.execute",
        capability_entrypoint="runtime.opencode.plugin:Handler.execute",
        request=_request(),
        attempt_root=AttemptRootDescriptor(attempt_directory_id="attempt-1"),
        activity_rpc=TaskActivityRpcIdentity(
            invocation_id="inv-1",
            task_id="task-1",
            activation_id="activation-run",
            attempt=1,
            activity_id=None,
        ),
        authorized_secret_handles=("opencode.token",),
    )


def _prepared_snapshot() -> TaskActivitySnapshot:
    return TaskActivitySnapshot(
        activity_id="activity-1",
        request_digest="0" * 64,
        workspace_identity=AttemptWorkspaceIdentity(
            attempt_directory_id="attempt-1",
            baseline_tree_id="0" * 64,
            attempt_identity_digest="1" * 64,
        ),
        state="prepared",
    )


class _FakeHost:
    async def execute(self, call: TaskHostExecuteCall) -> TaskHostCallResult:
        del call
        return TaskHostCallResult(operation="execute", outcome=TaskOutcome.succeeded({"ok": True}))

    async def reconcile(self, call: TaskHostReconcileCall) -> TaskHostCallResult:
        del call
        return TaskHostCallResult(
            operation="reconcile",
            reconcile_result=TaskActivityReconcileResult(status="not_dispatched"),
        )

    async def cancel(self, call: TaskHostCancelCall) -> TaskHostCallResult:
        del call
        return TaskHostCallResult(
            operation="cancel",
            cancel_result=TaskActivityCancelResult(status="acknowledged"),
        )

    def read_terminal_receipts(self, identity: TaskHostCallIdentity) -> tuple[TaskHostTerminalReceipt, ...]:
        del identity
        return ()


def test_secret_port_is_non_enumerable_and_rejects_unlocked_handle() -> None:
    port = _secret_port({"opencode.token": _CANARY})
    assert not hasattr(port, "keys")
    assert port.resolve("opencode.token") == _CANARY
    with pytest.raises(SecretHandleUnauthorized):
        port.resolve("cursor.token")


def test_secret_bytes_never_enter_host_call_projection() -> None:
    call = _execute_call()
    dumped = canonical_json_bytes(call.model_dump(mode="json"))
    assert b"opencode.token" in dumped
    assert _CANARY not in dumped
    assert b"canary-secret-value" not in dumped
    assert "worker_command" not in type(call).model_fields
    assert "codec" not in type(call).model_fields


def test_host_call_carries_resolved_capability_request_and_wire_identity() -> None:
    call = _execute_call()
    assert call.capability_id == "runtime.opencode.execute"
    assert call.capability_entrypoint == "runtime.opencode.plugin:Handler.execute"
    assert call.request.target_capability_id == "runtime.opencode.execute"
    assert call.attempt_root.capability_id == "graph.engine.attempt-root"
    assert "/" not in call.attempt_root.attempt_directory_id
    assert call.activity_rpc.activity_id is None
    assert call.authorized_secret_handles == ("opencode.token",)
    assert call.identity.wire_schema_version == "1"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        TaskHostExecuteCall.model_validate({**call.model_dump(mode="json"), "worker_command": "python"})


def test_task_execution_host_exposes_fixed_lifecycle_transport() -> None:
    host: TaskExecutionHost = _FakeHost()
    for name in ("execute", "reconcile", "cancel", "read_terminal_receipts"):
        assert callable(getattr(host, name))


def test_fake_host_lifecycle_methods_return_typed_results() -> None:
    host = _FakeHost()
    executed = asyncio.run(host.execute(_execute_call()))
    assert executed.operation == "execute"
    assert executed.outcome is not None
    assert executed.outcome.status == "succeeded"

    snapshot = _prepared_snapshot()
    reconcile_call = TaskHostReconcileCall(
        identity=_identity(operation="reconcile"),
        capability_id="runtime.opencode.execute",
        capability_entrypoint="runtime.opencode.plugin:Handler.execute",
        request=_request(),
        attempt_root=AttemptRootDescriptor(attempt_directory_id="attempt-1"),
        activity_rpc=TaskActivityRpcIdentity(
            invocation_id="inv-1",
            task_id="task-1",
            activation_id="activation-run",
            attempt=1,
        ),
        activity=snapshot,
        authorized_secret_handles=("opencode.token",),
    )
    reconciled = asyncio.run(host.reconcile(reconcile_call))
    assert reconciled.reconcile_result is not None
    assert reconciled.reconcile_result.status == "not_dispatched"

    cancel_call = TaskHostCancelCall(
        identity=_identity(operation="cancel"),
        capability_id="runtime.opencode.execute",
        capability_entrypoint="runtime.opencode.plugin:Handler.execute",
        request=_request(),
        attempt_root=AttemptRootDescriptor(attempt_directory_id="attempt-1"),
        activity_rpc=TaskActivityRpcIdentity(
            invocation_id="inv-1",
            task_id="task-1",
            activation_id="activation-run",
            attempt=1,
        ),
        activity=snapshot,
        authorized_secret_handles=("opencode.token",),
    )
    cancelled = asyncio.run(host.cancel(cancel_call))
    assert cancelled.cancel_result is not None
    assert cancelled.cancel_result.status == "acknowledged"
    assert host.read_terminal_receipts(_identity()) == ()


def test_phase2_task_context_may_omit_activity_and_secrets() -> None:
    context = TaskContext(
        workspace_root=Path("/workspace"),
        heartbeat=lambda: None,
        cancel_requested=lambda: False,
        invocation=_invocation(),
    )
    assert context.activity is None
    assert context.secrets is None
    assert set(context.__dataclass_fields__) == {
        "workspace_root",
        "heartbeat",
        "cancel_requested",
        "invocation",
        "activity",
        "secrets",
    }


def test_task_request_keeps_unique_sorted_resource_ids() -> None:
    request = TaskRequest(
        invocation_id="inv-1",
        task_id="task-1",
        graph_instance_id="graph-1",
        node_id="run",
        capability_id="fixture.agent.run",
        resource_ids=("fixture.result-schema", "fixture.instructions"),
        resource_digests={
            "fixture.instructions": "d" * 64,
            "fixture.result-schema": "e" * 64,
        },
        invocation=_invocation(),
        attempt=1,
        input=None,
    )
    assert request.resource_ids == ("fixture.instructions", "fixture.result-schema")
    with pytest.raises(ValidationError, match="resource digests"):
        TaskRequest(
            invocation_id="inv-1",
            task_id="task-1",
            graph_instance_id="graph-1",
            node_id="run",
            capability_id="fixture.agent.run",
            resource_ids=("fixture.instructions",),
            resource_digests={},
            invocation=_invocation(),
            attempt=1,
            input=None,
        )


def test_activity_port_protocol_exposes_snapshot_dispatch_and_bind() -> None:
    assert hasattr(TaskActivityPort, "mark_dispatch_started")
    assert hasattr(TaskActivityPort, "bind")
    assert getattr(TaskActivityPort, "snapshot", None) is not None


def test_runtime_exports_frozen_host_protocol_not_phase2_execute() -> None:
    import graph_engine.runtime as runtime
    from graph_engine.runtime.host_protocol import TaskExecutionHost as FrozenHost

    assert runtime.TaskExecutionHost is FrozenHost
    parameters = tuple(inspect.signature(runtime.TaskExecutionHost.execute).parameters)
    assert "call" in parameters
    assert "handler" not in parameters
    assert "workspace_root" not in parameters
