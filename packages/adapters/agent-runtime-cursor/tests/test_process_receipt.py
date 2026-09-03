from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
from pathlib import Path

from agent_runtime_contracts import (
    AgentRunRequest,
    AgentWorkspaceV1,
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
    InvocationMetadata,
    TaskActivitySnapshot,
    TaskContext,
    TaskRequest,
    TaskWorkspaceIdentity,
)


_SHA = "a" * 64
_CANARY = b"canary-secret-value"
_WRITE_ROOT = "qa/changes/CH-1/.staging/task-1/attempt-1"
_ALLOWED_OUTPUTS = ("qa/changes/CH-1/proposal.md",)


def _agent_workspace() -> AgentWorkspaceV1:
    payload = {
        "schema_version": "1",
        "agent_profile": "assurance-v1-doc-author",
        "scope_id": "CH-1",
        "write_root": _WRITE_ROOT,
        "allowed_outputs": _ALLOWED_OUTPUTS,
    }
    return AgentWorkspaceV1.model_validate({**payload, "identity_digest": canonical_digest(payload)})


def _workspace_identity() -> TaskWorkspaceIdentity:
    payload = {
        "task_id": "task-1",
        "attempt": 1,
        "attempt_id": "attempt-1",
        "output_paths": list(_ALLOWED_OUTPUTS),
        "baseline_files": [],
        "project_digest": "c" * 64,
        "write_root_digest": "d" * 64,
        "layout_schema_version": "1",
    }
    return TaskWorkspaceIdentity(**payload, identity_digest=canonical_digest(payload))


_WORKSPACE_IDENTITY = _workspace_identity()
_WORKSPACE_DIGEST = _WORKSPACE_IDENTITY.identity_digest
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
            "protocol_profile": "confined_process",
            "adapter_configuration_digest": _SHA,
        }
    )


def _binding_data(config: CursorAdapterConfig) -> dict[str, object]:
    return config.model_dump(mode="json")


def _agent_run() -> AgentRunRequest:
    return AgentRunRequest.model_validate(
        {
            "schema_version": "1",
            "instructions": (InstructionPart.text("text/plain", "write result.json"),),
            "result_contract": ResultContract(
                schema_id="fixture.result.v1",
                schema_digest=canonical_digest(_RESULT_SCHEMA),
                delivery_mode="assistant_json_local_v1",
            ),
            "execution": FrozenExecutionSelection(
                provider_model="provider_default",
                worker_profile="fixture-v1",
                permission_profile_digest="b" * 64,
                limits={"max_seconds": 120},  # type: ignore[arg-type]
            ),
            "workspace": _agent_workspace(),
            "request_policy_digest": "c" * 64,
            "request_config_digest": "d" * 64,
        }
    )


def _request(config: CursorAdapterConfig) -> TaskRequest:
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
            "binding_data": _binding_data(config),
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


def _spawned_receipt() -> tuple[CursorProcessReceipt, Path, Path]:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        write_root = (root / _WRITE_ROOT).resolve()
        write_root.mkdir(parents=True)
        port = FakeActivityPort(_prepared_snapshot())
        host = FakeConfinedProcessHost()
        config = _config(root)
        handler = CursorHandler(host)
        context = TaskContext(
            project_root=root.resolve(),
            write_root=write_root,
            workspace_identity=_WORKSPACE_IDENTITY,
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
        asyncio.run(handler.execute(_request(config), context))
        return (
            CursorProcessReceipt.model_validate(thaw_json(port.snapshot.reference)),
            root.resolve(),
            write_root,
        )


def test_process_receipt_binds_non_reusable_identity() -> None:
    receipt, project_root, write_root = _spawned_receipt()
    assert receipt.host_boot_identity_digest == BOOT_DIGEST
    assert receipt.confinement_identity
    assert receipt.process_start_token
    assert receipt.workspace_identity_digest == canonical_digest(
        {"project_root": str(project_root), "write_root": str(write_root)}
    )
    assert receipt.workspace_identity_digest != canonical_digest({"cwd": str(project_root)})
    assert receipt.workspace_identity_digest != _WORKSPACE_DIGEST
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
        config = _config(root)
        handler = CursorHandler(host)
        write_root = (root / _WRITE_ROOT).resolve()
        write_root.mkdir(parents=True)
        context = TaskContext(
            project_root=root.resolve(),
            write_root=write_root,
            workspace_identity=_WORKSPACE_IDENTITY,
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
        asyncio.run(handler.execute(_request(config), context))
        assert states == ["bound"]
        assert port.bind_calls
        bound = json.dumps(thaw_json(port.snapshot.reference))
        assert "canary" not in bound
        assert "api-key" not in bound
        assert "--resume" not in host.launches[0].argv


async def test_bound_receipt_rejects_dual_root_identity_drift(tmp_path: Path) -> None:
    from dataclasses import replace

    from cursor_harness import bind_spawned_fixture, workspace_identity  # pyright: ignore[reportMissingImports]

    fixture = await bind_spawned_fixture(tmp_path)
    drifted = replace(
        fixture.context,
        write_root=tmp_path / "other-stage",
        workspace_identity=workspace_identity(attempt_id="attempt-2"),
    )
    (tmp_path / "other-stage").mkdir()
    result = await fixture.handler.reconcile(fixture.request, drifted, fixture.activity)
    assert result.status == "indeterminate"
    assert result.reason is not None
    assert "workspace" in result.reason
