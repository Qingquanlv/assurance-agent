from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

import pytest
from pydantic import ValidationError

from graph_engine.attempts import production_worker
from graph_engine.canonical import canonical_digest, canonical_json_bytes
from graph_engine.plugin_api import (
    DirectoryIdentity,
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
    TaskWorkspaceIdentity,
)
from graph_engine.attempts.host_protocol import (
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
    current_bound_identity,
    identities_agree,
)
from graph_engine.attempts.workspace import TaskWorkspaceStore


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


def _bound(**overrides: object) -> dict[str, object]:
    workspace = _workspace_identity()
    fields = current_bound_identity(
        attempt_key_digest="a" * 64,
        authorization_id="b" * 64,
        workspace_identity_digest=workspace.identity_digest,
        request_digest=canonical_digest(_request().model_dump(mode="json")),
        graph_revision="c" * 64,
        product_lock_digest=_LOCK_DIGEST,
        handler_id="runtime.opencode.execute",
    )
    fields.update(overrides)
    return fields


def _identity(*, operation: str = "execute") -> TaskHostCallIdentity:
    return TaskHostCallIdentity(
        invocation_id="inv-1",
        task_id="task-1",
        activation_id="activation-run",
        attempt=1,
        activity_id=None,
        operation=operation,  # type: ignore[arg-type]
        **_bound(),  # type: ignore[arg-type]
    )


def _fake_directory_identity(path: str, inode: int) -> DirectoryIdentity:
    payload = {
        "path_digest": canonical_digest({"path": path}),
        "device": 1,
        "inode": inode,
    }
    return DirectoryIdentity(**payload, identity_digest=canonical_digest(payload))


def _workspace_identity() -> TaskWorkspaceIdentity:
    project = _fake_directory_identity("/project", 1)
    write = _fake_directory_identity("/attempts/task-1/attempt-1", 2)
    payload = {
        "task_id": "task-1",
        "attempt": 1,
        "attempt_id": "attempt-1",
        "output_paths": ["out.txt"],
        "baseline_files": [],
        "project_digest": project.identity_digest,
        "write_root_digest": write.identity_digest,
        "layout_schema_version": "1",
    }
    return TaskWorkspaceIdentity(identity_digest=canonical_digest(payload), **payload)


def _attempt_root() -> AttemptRootDescriptor:
    workspace = _workspace_identity()
    project = _fake_directory_identity("/project", 1)
    write = _fake_directory_identity("/attempts/task-1/attempt-1", 2)
    return AttemptRootDescriptor(
        workspace_identity=workspace,
        project_root_identity=project,
        write_root_identity=write,
        project_root_digest=workspace.project_digest,
        write_root_digest=workspace.write_root_digest,
        baseline_digest=canonical_digest([]),
    )


def _execute_call() -> TaskHostExecuteCall:
    return TaskHostExecuteCall(
        identity=_identity(),
        capability_id="runtime.opencode.execute",
        capability_entrypoint="runtime.opencode.plugin:Handler.execute",
        request=_request(),
        attempt_root=_attempt_root(),
        activity_rpc=TaskActivityRpcIdentity(
            invocation_id="inv-1",
            task_id="task-1",
            activation_id="activation-run",
            attempt=1,
            activity_id=None,
            **_bound(),  # type: ignore[arg-type]
        ),
        authorized_secret_handles=("opencode.token",),
    )


def _prepared_snapshot() -> TaskActivitySnapshot:
    return TaskActivitySnapshot(
        activity_id="activity-1",
        request_digest="0" * 64,
        workspace_identity=_workspace_identity(),
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
    assert call.attempt_root.schema_version == "2"
    assert call.attempt_root.workspace_identity == _workspace_identity()
    assert call.activity_rpc.activity_id is None
    assert call.authorized_secret_handles == ("opencode.token",)
    assert call.identity.wire_schema_version == "2"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        TaskHostExecuteCall.model_validate({**call.model_dump(mode="json"), "worker_command": "python"})


def test_schema_v2_attempt_root_authenticates_both_roots_and_baseline(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / "out.txt").write_text("before", encoding="utf-8")
    store = TaskWorkspaceStore(
        project_root,
        tmp_path / "attempts",
        tmp_path / "promotion-receipts",
    )
    try:
        workspace = store.begin(task_id="task-1", attempt=1, output_paths=("out.txt",))
        baseline_digest = canonical_digest(
            [item.model_dump(mode="json") for item in workspace.identity.baseline_files]
        )
        descriptor = AttemptRootDescriptor.model_validate(
            {
                "schema_version": "2",
                "workspace_identity": workspace.identity.model_dump(mode="json"),
                "project_root_identity": workspace.project_root_identity.model_dump(mode="json"),
                "write_root_identity": workspace.write_root_identity.model_dump(mode="json"),
                "project_root_digest": workspace.identity.project_digest,
                "write_root_digest": workspace.identity.write_root_digest,
                "baseline_digest": baseline_digest,
            }
        )
    finally:
        store.close()

    assert descriptor.schema_version == "2"
    assert descriptor.workspace_identity == workspace.identity
    assert descriptor.project_root_digest == workspace.identity.project_digest
    assert descriptor.write_root_digest == workspace.identity.write_root_digest
    assert descriptor.baseline_digest == baseline_digest
    assert "attempt_directory_id" not in descriptor.model_dump(mode="json")


@pytest.mark.parametrize("changed", ["project_root", "write_root"])
def test_worker_rejects_same_path_root_replacement_before_handler_execution(
    tmp_path: Path,
    changed: str,
) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    store = TaskWorkspaceStore(project_root, tmp_path / "attempts", tmp_path / "receipts")
    executed = False

    class MustNotRun:
        async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
            nonlocal executed
            del request, context
            executed = True
            return TaskOutcome.succeeded()

    try:
        binding = store.begin(task_id="task-1", attempt=1, output_paths=("out.txt",))
        descriptor = AttemptRootDescriptor(
            workspace_identity=binding.identity,
            project_root_identity=binding.project_root_identity,
            write_root_identity=binding.write_root_identity,
            project_root_digest=binding.identity.project_digest,
            write_root_digest=binding.identity.write_root_digest,
            baseline_digest=canonical_digest(
                [item.model_dump(mode="json") for item in binding.identity.baseline_files]
            ),
        )
        call = _execute_call().model_copy(update={"attempt_root": descriptor})
        original = getattr(binding, changed)
        parked = original.with_name(f"{original.name}-parked")
        original.rename(parked)
        original.mkdir()

        with pytest.raises(Exception, match="root|identity|descriptor|authenticated"):
            asyncio.run(
                production_worker._run_call(
                    MustNotRun(),
                    "execute",
                    call,
                    project_root=binding.project_root,
                    write_root=binding.write_root,
                    secrets=None,
                    activity_port=None,
                    cancel_requested=lambda: False,
                )
            )
    finally:
        store.close()

    assert executed is False


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
        attempt_root=_attempt_root(),
        activity_rpc=TaskActivityRpcIdentity(
            invocation_id="inv-1",
            task_id="task-1",
            activation_id="activation-run",
            attempt=1,
            **_bound(),  # type: ignore[arg-type]
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
        attempt_root=_attempt_root(),
        activity_rpc=TaskActivityRpcIdentity(
            invocation_id="inv-1",
            task_id="task-1",
            activation_id="activation-run",
            attempt=1,
            **_bound(),  # type: ignore[arg-type]
        ),
        activity=snapshot,
        authorized_secret_handles=("opencode.token",),
    )
    cancelled = asyncio.run(host.cancel(cancel_call))
    assert cancelled.cancel_result is not None
    assert cancelled.cancel_result.status == "acknowledged"
    assert host.read_terminal_receipts(_identity()) == ()


def test_task_context_exposes_authenticated_project_and_write_roots(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    store = TaskWorkspaceStore(project_root, tmp_path / "attempts", tmp_path / "receipts")
    try:
        binding = store.begin(task_id="task-1", attempt=1, output_paths=("out.txt",))
        context = TaskContext(
            project_root=binding.project_root,
            write_root=binding.write_root,
            workspace_identity=binding.identity,
            heartbeat=lambda: None,
            cancel_requested=lambda: False,
            invocation=_invocation(),
        )
    finally:
        store.close()

    assert context.project_root == project_root.resolve()
    assert context.write_root == binding.write_root
    assert context.workspace_identity == binding.identity
    assert context.activity is None
    assert context.secrets is None
    assert set(context.__dataclass_fields__) == {
        "project_root",
        "write_root",
        "workspace_identity",
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
    from graph_engine.attempts import host_protocol as runtime
    from graph_engine.attempts.host_protocol import TaskExecutionHost as FrozenHost

    assert runtime.TaskExecutionHost is FrozenHost
    parameters = tuple(inspect.signature(runtime.TaskExecutionHost.execute).parameters)
    assert "call" in parameters
    assert "handler" not in parameters
    assert "workspace_root" not in parameters


@pytest.mark.parametrize(
    "field,value",
    [
        ("wire_schema_version", "1"),
        ("attempt_key_digest", "0" * 64),
        ("authorization_id", "1" * 64),
        ("fencing_token", 99),
        ("phase", "prepare"),
        ("workspace_identity_digest", "2" * 64),
        ("request_digest", "3" * 64),
        ("graph_revision", "4" * 64),
        ("product_lock_digest", "5" * 64),
        ("handler_id", "runtime.other.execute"),
        ("host_implementation_digest", "6" * 64),
    ],
)
def test_prior_or_mismatched_rpc_identity_is_rejected(field: str, value: object) -> None:
    payload = TaskActivityRpcIdentity(
        invocation_id="inv-1",
        task_id="task-1",
        activation_id="activation-run",
        attempt=1,
        activity_id="activity-1",
        **_bound(),  # type: ignore[arg-type]
    ).model_dump(mode="json")
    payload[field] = value
    if field == "wire_schema_version":
        with pytest.raises(ValidationError):
            TaskActivityRpcIdentity.model_validate(payload)
        return
    other = TaskActivityRpcIdentity.model_validate(payload)
    assert not identities_agree(
        other,
        TaskActivityRpcIdentity(
            invocation_id="inv-1",
            task_id="task-1",
            activation_id="activation-run",
            attempt=1,
            activity_id="activity-1",
            **_bound(),  # type: ignore[arg-type]
        ),
    )


def test_stale_fence_is_not_the_current_identity() -> None:
    current = TaskHostCallIdentity(
        invocation_id="inv-1",
        task_id="task-1",
        activation_id="activation-run",
        attempt=1,
        activity_id="activity-1",
        operation="execute",
        **_bound(),  # type: ignore[arg-type]
    )
    stale = TaskHostCallIdentity(
        invocation_id="inv-1",
        task_id="task-1",
        activation_id="activation-run",
        attempt=1,
        activity_id="activity-1",
        operation="execute",
        **_bound(fencing_token=1),  # type: ignore[arg-type]
    )
    newer = TaskHostCallIdentity(
        invocation_id="inv-1",
        task_id="task-1",
        activation_id="activation-run",
        attempt=1,
        activity_id="activity-1",
        operation="execute",
        **_bound(fencing_token=2),  # type: ignore[arg-type]
    )
    assert current.fencing_token == 1
    assert not identities_agree(stale, newer)
