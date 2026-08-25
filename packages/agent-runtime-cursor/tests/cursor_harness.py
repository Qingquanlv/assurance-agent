from __future__ import annotations

import hashlib
import json
import tempfile
from dataclasses import dataclass
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
from agent_runtime_cursor.process import (
    authenticate_confinement,
    authenticate_executable,
    build_launch_request,
    cursor_dispatch_fingerprint,
)
from fake_process_host import FakeActivityPort, FakeConfinedProcessHost  # pyright: ignore[reportMissingImports]
from graph_engine.plugin_api import (
    InvocationMetadata,
    SecretHandleUnauthorized,
    SecretPort,
    TaskActivityReconcileResult,
    TaskActivitySnapshot,
    TaskContext,
    TaskRequest,
    TaskWorkspaceIdentity,
)


SHA = "a" * 64
CANARY = b"canary-secret-value"
SECRET_TEXT = "canary-secret-value"
WRITE_ROOT = "qa/changes/CH-1/.staging/task-1/attempt-1"
ALLOWED_OUTPUTS = ("qa/changes/CH-1/proposal.md",)
RESULT_SCHEMA = {
    "additionalProperties": False,
    "properties": {"ok": {"const": True, "type": "boolean"}},
    "required": ["ok"],
    "type": "object",
}


def agent_workspace(
    *,
    write_root: str = WRITE_ROOT,
    allowed_outputs: tuple[str, ...] = ALLOWED_OUTPUTS,
) -> AgentWorkspaceV1:
    payload = {
        "schema_version": "1",
        "write_root": write_root,
        "allowed_outputs": allowed_outputs,
    }
    return AgentWorkspaceV1.model_validate({**payload, "identity_digest": canonical_digest(payload)})


def workspace_identity(
    *,
    task_id: str = "task-1",
    attempt: int = 1,
    attempt_id: str = "attempt-1",
    output_paths: tuple[str, ...] = ALLOWED_OUTPUTS,
) -> TaskWorkspaceIdentity:
    payload = {
        "task_id": task_id,
        "attempt": attempt,
        "attempt_id": attempt_id,
        "output_paths": list(output_paths),
        "baseline_files": [],
        "project_digest": "c" * 64,
        "write_root_digest": "d" * 64,
        "layout_schema_version": "1",
    }
    return TaskWorkspaceIdentity(**payload, identity_digest=canonical_digest(payload))


WORKSPACE_IDENTITY = workspace_identity()


def write_cursor_bin(root: Path) -> str:
    path = (root / "cursor").resolve()
    path.write_bytes(b"cursor-binary")
    path.chmod(0o755)
    return str(path)


def config(root: Path, **overrides: object) -> CursorAdapterConfig:
    executable = str(overrides.pop("executable", write_cursor_bin(root)))
    payload: dict[str, object] = {
        "schema_version": "1",
        "executable": executable,
        "executable_digest": hashlib.sha256(Path(executable).read_bytes()).hexdigest()
        if Path(executable).is_file()
        else SHA,
        "expected_version": "1.0.0",
        "secret_handle": "cursor.api-key",
        "environment_names": ["PATH", "CURSOR_API_KEY"],
        "graceful_cancel_seconds": 5,
        "forced_cancel_seconds": 10,
        "max_output_bytes": 65536,
        "max_line_bytes": 4096,
        "protocol_profile": "confined_process",
        "adapter_configuration_digest": SHA,
    }
    payload.update(overrides)
    return CursorAdapterConfig.model_validate(payload)


def binding_data(config: CursorAdapterConfig) -> dict[str, object]:
    return config.model_dump(mode="json")


def agent_run(**overrides: object) -> AgentRunRequest:
    payload: dict[str, object] = {
        "schema_version": "1",
        "instructions": (InstructionPart.text("text/plain", "write result.json"),),
        "result_contract": ResultContract(
            schema_id="fixture.result.v1",
            schema_digest=canonical_digest(RESULT_SCHEMA),
            extraction_mode="structured",
        ),
        "execution": FrozenExecutionSelection(
            provider_model="provider_default",
            worker_profile="fixture-v1",
            permission_profile_digest="b" * 64,
            limits={"max_seconds": 120},  # type: ignore[arg-type]
        ),
        "workspace": agent_workspace(),
        "request_policy_digest": "c" * 64,
        "request_config_digest": "d" * 64,
    }
    payload.update(overrides)
    return AgentRunRequest.model_validate(payload)


def request(**overrides: object) -> TaskRequest:
    run = overrides.pop("agent_run", agent_run())
    assert isinstance(run, AgentRunRequest)
    payload: dict[str, object] = {
        "invocation_id": "inv-1",
        "task_id": "task-1",
        "graph_instance_id": "graph-1",
        "node_id": "run",
        "capability_id": "runtime.cursor.execute",
        "invocation": InvocationMetadata(
            invocation_id="inv-1",
            lock_digest=SHA,
            composition_digest="b" * 64,
            entrypoint="runtime.cursor.execute",
        ),
        "attempt": 1,
        "input": run.model_dump(mode="json"),
        "binding_data": {},
    }
    payload.update(overrides)
    return TaskRequest.model_validate(payload)


class ExactSecretPort:
    def __init__(self, authorized: dict[str, bytes] | None = None) -> None:
        self._authorized = dict(authorized) if authorized is not None else {"cursor.api-key": CANARY}

    def resolve(self, handle: str) -> bytes:
        try:
            return bytes(self._authorized[handle])
        except KeyError as error:
            raise SecretHandleUnauthorized(f"unauthorized secret handle: {handle}") from error


class RevocableSecretPort:
    def __init__(self, authorized: dict[str, bytes] | None = None) -> None:
        self._authorized = dict(authorized) if authorized is not None else {"cursor.api-key": CANARY}
        self.revoked = False
        self.resolved_handles: list[str] = []

    def resolve(self, handle: str) -> bytes:
        if self.revoked:
            raise SecretHandleUnauthorized("secret port is revoked")
        self.resolved_handles.append(handle)
        try:
            return bytes(self._authorized[handle])
        except KeyError as error:
            raise SecretHandleUnauthorized(f"unauthorized secret handle: {handle}") from error

    def revoke(self) -> None:
        self.revoked = True


def prepared_snapshot() -> TaskActivitySnapshot:
    return TaskActivitySnapshot(
        activity_id="activity-1",
        request_digest="e" * 64,
        workspace_identity=WORKSPACE_IDENTITY,
        state="prepared",
    )


def context(
    root: Path,
    *,
    port: FakeActivityPort | None = None,
    secrets: SecretPort | None = None,
    write_root: Path | None = None,
    identity: TaskWorkspaceIdentity | None = None,
) -> tuple[TaskContext, FakeActivityPort]:
    project_root = root.resolve()
    stage = (write_root or (project_root / WRITE_ROOT)).resolve()
    stage.mkdir(parents=True, exist_ok=True)
    activity = port if port is not None else FakeActivityPort(prepared_snapshot())
    return (
        TaskContext(
            project_root=project_root,
            write_root=stage,
            workspace_identity=identity if identity is not None else workspace_identity(),
            heartbeat=lambda: None,
            cancel_requested=lambda: False,
            invocation=InvocationMetadata(
                invocation_id="inv-1",
                lock_digest=SHA,
                composition_digest="b" * 64,
                entrypoint="runtime.cursor.execute",
            ),
            activity=activity,
            secrets=secrets if secrets is not None else ExactSecretPort(),
        ),
        activity,
    )


def encode_stream(events: list[dict[str, object]]) -> bytes:
    return b"".join(json.dumps(event, separators=(",", ":")).encode("utf-8") + b"\n" for event in events)


def complete_stream(
    cwd: str,
    *,
    result: object = None,
    session_id: str = "sess-1",
    version: str | None = None,
) -> bytes:
    structured = {"ok": True} if result is None else result
    init: dict[str, object] = {"cwd": cwd, "session_id": session_id, "subtype": "init", "type": "system"}
    if version is not None:
        init["version"] = version
    return encode_stream(
        [
            init,
            {
                "message": {"content": [{"text": "working", "type": "text"}], "role": "assistant"},
                "session_id": session_id,
                "type": "assistant",
            },
            {"session_id": session_id, "subtype": "started", "tool": "write", "type": "tool_call"},
            {
                "is_error": False,
                "result": structured,
                "session_id": session_id,
                "subtype": "success",
                "type": "result",
            },
        ],
    )


def error_stream(cwd: str, *, message: str = "provider exploded") -> bytes:
    return encode_stream(
        [
            {"cwd": cwd, "session_id": "sess-1", "subtype": "init", "type": "system"},
            {
                "error": message,
                "is_error": True,
                "session_id": "sess-1",
                "subtype": "error",
                "type": "result",
            },
        ],
    )


def init_only_stream(cwd: str) -> bytes:
    return encode_stream(
        [{"cwd": cwd, "session_id": "sess-1", "subtype": "init", "type": "system"}],
    )


@dataclass
class CursorFixture:
    handler: CursorHandler
    host: FakeConfinedProcessHost
    port: FakeActivityPort
    request: TaskRequest
    context: TaskContext
    config: CursorAdapterConfig

    @property
    def activity(self) -> TaskActivitySnapshot:
        return self.port.snapshot

    @property
    def spawn_count(self) -> int:
        return self.host.spawn_count

    async def execute_until_cut(self) -> None:
        try:
            await self.handler.execute(self.request, self.context)
        except Exception:
            return

    async def reconcile_after_restart(self) -> TaskActivityReconcileResult:
        restarted = CursorHandler(self.host)
        result = await restarted.reconcile(self.request, self.context, self.activity)
        assert isinstance(result, TaskActivityReconcileResult)
        return result


def execute_fixture(
    root: Path,
    host: FakeConfinedProcessHost | None = None,
    *,
    agent_run: AgentRunRequest | None = None,
) -> CursorFixture:
    cfg = config(root)
    process_host = host if host is not None else FakeConfinedProcessHost()
    if process_host.stdout is None:
        process_host.stdout = complete_stream(str(root.resolve()))
    task_context, port = context(root)
    task = request(
        binding_data=binding_data(cfg), **({"agent_run": agent_run} if agent_run is not None else {})
    )
    return CursorFixture(
        handler=CursorHandler(process_host),
        host=process_host,
        port=port,
        request=task,
        context=task_context,
        config=cfg,
    )


async def bind_spawned_fixture(
    root: Path,
    *,
    host: FakeConfinedProcessHost | None = None,
) -> CursorFixture:
    fixture = execute_fixture(root, host)
    executable = authenticate_executable(fixture.config)
    agent = AgentRunRequest.model_validate(thaw_json(fixture.request.input))
    launch = build_launch_request(fixture.config, agent, fixture.context, executable)
    identity = fixture.host.preflight(launch)
    authenticate_confinement(identity, expected_version=fixture.config.expected_version)
    fingerprint = cursor_dispatch_fingerprint(
        fixture.config,
        launch.argv,
        request_digest=launch.request_digest,
        workspace_identity_digest=launch.workspace_identity_digest,
        host_boot_identity_digest=identity.host_boot_identity_digest,
        host_instance_id=identity.host_instance_id,
        attempt=fixture.request.attempt,
        task_id=fixture.request.task_id,
    )
    fixture.port.mark_dispatch_started(fingerprint)
    process = await fixture.host.spawn(launch)
    fixture.port.bind(process.receipt.model_dump(mode="json"))
    return fixture


def _host_for_cut(cut: str, cwd: str) -> FakeConfinedProcessHost:
    if cut == "before_spawn":
        return FakeConfinedProcessHost(cut="before_spawn")
    if cut == "after_spawn_before_bind":
        return FakeConfinedProcessHost(cut="after_spawn_before_bind")
    if cut == "after_bind":
        return FakeConfinedProcessHost(cut="after_bind", status="running", exit_code=None)
    if cut == "mid_stream":
        return FakeConfinedProcessHost(cut="mid_stream", stdout=complete_stream(cwd))
    if cut == "after_host_terminal_receipt":
        return FakeConfinedProcessHost(cut="after_host_terminal_receipt", stdout=complete_stream(cwd))
    if cut == "after_process_exit_without_terminal":
        return FakeConfinedProcessHost(stdout=init_only_stream(cwd), exit_code=0)
    raise AssertionError(cut)


async def cursor_cut(cut: str) -> CursorFixture:
    root = Path(tempfile.mkdtemp())
    host = _host_for_cut(cut, str(root.resolve()))
    fixture = execute_fixture(root, host)
    await fixture.execute_until_cut()
    return fixture
