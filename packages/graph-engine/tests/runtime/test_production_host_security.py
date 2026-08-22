from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from graph_engine.canonical import canonical_digest, canonical_json_bytes
from graph_engine.composition.lock import pinned_execution_host_lock
from graph_engine.plugin_api import (
    InvocationMetadata,
    ResourceClaims,
    TaskContext,
    TaskOutcome,
    TaskRequest,
)
from graph_engine.runtime.host_protocol import (
    AttemptRootDescriptor,
    TaskActivityRpcIdentity,
    TaskHostCallIdentity,
    TaskHostExecuteCall,
    TaskHostProtocolError,
    scan_for_secret_leaks,
)
from graph_engine.runtime.production_host import ProductionHostError, _ProcessSupervisor, _ProductionTaskExecutionHost
from graph_engine.runtime.secret_sources import (
    InvocationRuntimeAuthorization,
    SecretSourceBinding,
    empty_runtime_authorization,
    runtime_authorization_digest,
)
from graph_engine.runtime.workspace import SnapshotStore


_CANARY = b"canary-secret-material"


class _SecretEchoHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del request
        assert context.secrets is not None
        value = context.secrets.resolve("test.secret")
        return TaskOutcome.succeeded({"fingerprint": canonical_digest({"secret": value.decode("utf-8")})})


def _authorization(secret_path: Path) -> InvocationRuntimeAuthorization:
    binding = SecretSourceBinding(
        handle="test.secret",
        source_kind="file",
        source_locator=str(secret_path.resolve()),
    )
    return InvocationRuntimeAuthorization(
        schema_version="1",
        secret_sources=(binding,),
        digest=runtime_authorization_digest((binding,)),
    )


def _execute_call(*, secret_handles: tuple[str, ...] = ("test.secret",)) -> TaskHostExecuteCall:
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
        capability_id="test.secret.run",
        capability_entrypoint="tests.runtime.test_production_host_security:_SecretEchoHandler.execute",
        request=TaskRequest(
            invocation_id="inv-1",
            task_id="task-1",
            graph_instance_id="graph-1",
            node_id="run",
            capability_id="test.secret.run",
            target_capability_id="test.secret.run",
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
        ),
        attempt_root=AttemptRootDescriptor(attempt_directory_id="attempt-1"),
        activity_rpc=TaskActivityRpcIdentity(
            invocation_id="inv-1",
            task_id="task-1",
            activation_id="activation-run",
            attempt=1,
            activity_id=None,
        ),
        authorized_secret_handles=secret_handles,
    )


def test_scan_for_secret_leaks_detects_canary() -> None:
    with pytest.raises(TaskHostProtocolError, match="leaked"):
        scan_for_secret_leaks(f"prefix {_CANARY.decode()} suffix", (_CANARY,))


def test_production_spawn_never_puts_secret_in_worker_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret_path = tmp_path / "secret.txt"
    secret_path.write_bytes(_CANARY)
    captured: dict[str, object] = {}

    class _RecordingPopen:
        args: tuple[object, ...]
        kwargs: dict[str, object]

        def __init__(self, *args: object, **kwargs: object) -> None:
            self.args = args
            self.kwargs = kwargs
            captured["args"] = args
            captured["kwargs"] = kwargs
            self.stdin = None
            self.stdout = None
            self.stderr = None
            self.pid = 4242

        def poll(self) -> int | None:
            return 0

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            return 0

    monkeypatch.setattr("graph_engine.runtime.production_host.subprocess.Popen", _RecordingPopen)
    supervisor = _ProcessSupervisor.for_platform()
    supervisor.spawn(attempt_root=tmp_path / "attempt", call_digest="d" * 64)
    kwargs = captured["kwargs"]
    argv = kwargs["args"] if "args" in kwargs else captured["args"]
    env = kwargs["env"]
    serialized = canonical_json_bytes({"argv": list(argv), "env": env})
    scan_for_secret_leaks(serialized, (_CANARY,))


def test_production_host_redacts_secrets_from_response(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    handler_path = tmp_path / "secret_handler.py"
    handler_path.write_text(
        """
from graph_engine.canonical import canonical_digest
from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest

class Handler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del request
        assert context.secrets is not None
        value = context.secrets.resolve("test.secret")
        return TaskOutcome.succeeded({"fingerprint": canonical_digest({"secret": value.decode("utf-8")})})
""",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    secret_path = tmp_path / "secret.txt"
    secret_path.write_bytes(_CANARY)
    authorization = _authorization(secret_path)
    store = SnapshotStore.create(tmp_path / "workspace", {})
    store.create_attempt("attempt-1")
    host = _ProductionTaskExecutionHost(root=tmp_path, authorization=authorization)
    host.bind_invocation_runtime(handlers={"test.secret.run": _SecretEchoHandler()}, store=store)
    call = _execute_call().model_copy(
        update={"capability_entrypoint": "secret_handler:Handler.execute"}
    )
    result = asyncio.run(host.execute(call))
    assert result.outcome is not None
    payload = canonical_json_bytes(result.model_dump(mode="json"))
    scan_for_secret_leaks(payload, (_CANARY,))


def test_production_host_rejects_unauthorized_secret_handle(tmp_path: Path) -> None:
    store = SnapshotStore.create(tmp_path / "workspace", {})
    store.create_attempt("attempt-1")
    host = _ProductionTaskExecutionHost(root=tmp_path, authorization=empty_runtime_authorization())
    host.bind_invocation_runtime(handlers={"test.secret.run": _SecretEchoHandler()}, store=store)
    with pytest.raises(ProductionHostError, match="missing authorized secret handle"):
        asyncio.run(host.execute(_execute_call(secret_handles=("test.secret",))))
