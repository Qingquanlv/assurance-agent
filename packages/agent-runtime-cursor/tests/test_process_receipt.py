from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
from pathlib import Path

from agent_runtime_contracts import (
    AgentRunRequest,
    FrozenExecutionSelection,
    InstructionPart,
    ResultContract,
)
from agent_runtime_contracts.schema import canonical_digest, thaw_json
from agent_runtime_cursor.config import CursorAdapterConfig
from agent_runtime_cursor.handler import CursorHandler
from agent_runtime_cursor.process import CursorProcessReceipt
from fake_process_host import BOOT_DIGEST, FakeActivityPort, FakeConfinedProcessHost  # pyright: ignore[reportMissingImports]
from graph_engine.plugin_api import (
    AttemptWorkspaceIdentity,
    InvocationMetadata,
    TaskActivitySnapshot,
    TaskContext,
    TaskRequest,
)


_SHA = "a" * 64
_CANARY = b"canary-secret-value"
_WORKSPACE_IDENTITY = AttemptWorkspaceIdentity(
    attempt_directory_id="attempt-1",
    baseline_tree_id="c" * 64,
    attempt_identity_digest="d" * 64,
)
_WORKSPACE_DIGEST = canonical_digest(_WORKSPACE_IDENTITY.model_dump(mode="json"))
_RESULT_SCHEMA = {
    "additionalProperties": False,
    "properties": {"ok": {"const": True, "type": "boolean"}},
    "required": ["ok"],
    "type": "object",
}


def _write_cursor_bin(root: Path) -> str:
    path = (root / "cursor").resolve()
    path.write_bytes(b"cursor-binary")
    path.chmod(0o755)
    return str(path)


def _config(root: Path) -> CursorAdapterConfig:
    executable = _write_cursor_bin(root)
    return CursorAdapterConfig.model_validate(
        {
            "schema_version": "1",
            "executable": executable,
            "executable_digest": hashlib.sha256(Path(executable).read_bytes()).hexdigest(),
            "expected_version": "1.0.0",
            "secret_handle": "cursor.api-key",
            "environment_names": ["PATH", "CURSOR_API_KEY"],
            "graceful_cancel_seconds": 5,
            "forced_cancel_seconds": 10,
            "max_output_bytes": 65536,
            "max_line_bytes": 4096,
        }
    )


def _agent_run() -> AgentRunRequest:
    return AgentRunRequest.model_validate(
        {
            "schema_version": "1",
            "instructions": (InstructionPart.text("text/plain", "write result.json"),),
            "result_contract": ResultContract(
                schema_id="fixture.result.v1",
                schema_digest=canonical_digest(_RESULT_SCHEMA),
                extraction_mode="structured",
            ),
            "execution": FrozenExecutionSelection(
                provider_model="provider_default",
                worker_profile="fixture-v1",
                permission_profile_digest="b" * 64,
                limits={"max_seconds": 120},  # type: ignore[arg-type]
            ),
            "request_policy_digest": "c" * 64,
            "request_config_digest": "d" * 64,
        }
    )


def _request() -> TaskRequest:
    agent_run = _agent_run()
    return TaskRequest.model_validate(
        {
            "invocation_id": "inv-1",
            "task_id": "task-1",
            "graph_instance_id": "graph-1",
            "node_id": "run",
            "capability_id": "runtime.cursor.execute",
            "invocation": InvocationMetadata(
                invocation_id="inv-1",
                lock_digest=_SHA,
                composition_digest="b" * 64,
                entrypoint="runtime.cursor.execute",
            ),
            "attempt": 1,
            "input": agent_run.model_dump(mode="json"),
            "binding_data": {"result_schema": _RESULT_SCHEMA},
        }
    )


class _ExactSecretPort:
    def resolve(self, handle: str) -> bytes:
        del handle
        return _CANARY


def _prepared_snapshot() -> TaskActivitySnapshot:
    return TaskActivitySnapshot(
        activity_id="activity-1",
        request_digest="e" * 64,
        workspace_identity=_WORKSPACE_IDENTITY,
        state="prepared",
    )


def _spawned_receipt() -> CursorProcessReceipt:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        port = FakeActivityPort(_prepared_snapshot())
        host = FakeConfinedProcessHost()
        handler = CursorHandler(_config(root), host)
        context = TaskContext(
            workspace_root=root.resolve(),
            heartbeat=lambda: None,
            cancel_requested=lambda: False,
            invocation=InvocationMetadata(
                invocation_id="inv-1",
                lock_digest=_SHA,
                composition_digest="b" * 64,
                entrypoint="runtime.cursor.execute",
            ),
            activity=port,
            secrets=_ExactSecretPort(),
        )
        asyncio.run(handler.execute(_request(), context))
        return CursorProcessReceipt.model_validate(thaw_json(port.snapshot.reference))


def test_process_receipt_binds_non_reusable_identity() -> None:
    receipt = _spawned_receipt()
    assert receipt.host_boot_identity_digest == BOOT_DIGEST
    assert receipt.confinement_identity
    assert receipt.process_start_token
    assert receipt.workspace_identity_digest == _WORKSPACE_DIGEST
    assert "api-key" not in receipt.model_dump_json()


def test_process_receipt_is_bound_before_stream_consumption() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        port = FakeActivityPort(_prepared_snapshot())
        states: list[str] = []

        def _on_wait(receipt: CursorProcessReceipt) -> None:
            del receipt
            states.append(port.snapshot.state)

        host = FakeConfinedProcessHost(wait_hook=_on_wait)
        handler = CursorHandler(_config(root), host)
        context = TaskContext(
            workspace_root=root.resolve(),
            heartbeat=lambda: None,
            cancel_requested=lambda: False,
            invocation=InvocationMetadata(
                invocation_id="inv-1",
                lock_digest=_SHA,
                composition_digest="b" * 64,
                entrypoint="runtime.cursor.execute",
            ),
            activity=port,
            secrets=_ExactSecretPort(),
        )
        asyncio.run(handler.execute(_request(), context))
        assert states == ["bound"]
        assert port.bind_calls
        bound = json.dumps(thaw_json(port.snapshot.reference))
        assert "canary" not in bound
        assert "api-key" not in bound
        assert "--resume" not in host.launches[0].argv
