from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest
from agent_runtime_contracts import (
    AgentRunRequest,
    FrozenExecutionSelection,
    InstructionPart,
    ResultContract,
)
from agent_runtime_contracts.schema import canonical_digest, canonical_json_bytes
from agent_runtime_cursor.config import CursorAdapterConfig
from agent_runtime_cursor.handler import CursorHandler
from fake_process_host import FakeConfinedProcessHost  # pyright: ignore[reportMissingImports]
from graph_engine import TaskActivityProtocolViolation
from graph_engine.plugin_api import (
    InvocationMetadata,
    SecretHandleUnauthorized,
    SecretPort,
    TaskContext,
    TaskRequest,
)
from pydantic import ValidationError


_SHA = "a" * 64
_CANARY = b"canary-secret-value"
_SECRET_TEXT = "canary-secret-value"
_CURSOR_BIN_NAME = "cursor"


def _write_cursor_bin(root: Path, content: bytes = b"cursor-binary") -> str:
    path = (root / _CURSOR_BIN_NAME).resolve()
    path.write_bytes(content)
    path.chmod(0o755)
    return str(path)


def _config(tmp_path: Path, **overrides: object) -> CursorAdapterConfig:
    executable = str(overrides.pop("executable", _write_cursor_bin(tmp_path)))
    payload: dict[str, object] = {
        "schema_version": "1",
        "executable": executable,
        "executable_digest": hashlib.sha256(Path(executable).read_bytes()).hexdigest()
        if Path(executable).is_file()
        else _SHA,
        "expected_version": "1.0.0",
        "secret_handle": "cursor.api-key",
        "environment_names": ["PATH", "CURSOR_API_KEY"],
        "graceful_cancel_seconds": 5,
        "forced_cancel_seconds": 10,
        "max_output_bytes": 65536,
        "max_line_bytes": 4096,
    }
    payload.update(overrides)
    return CursorAdapterConfig.model_validate(payload)


def _agent_run(**overrides: object) -> AgentRunRequest:
    payload: dict[str, object] = {
        "schema_version": "1",
        "instructions": (InstructionPart.text("text/plain", "write result.json"),),
        "result_contract": ResultContract(
            schema_id="fixture.result.v1",
            schema_digest=_SHA,
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
    payload.update(overrides)
    return AgentRunRequest.model_validate(payload)


def _request(tmp_path: Path | None = None, **overrides: object) -> TaskRequest:
    del tmp_path
    agent_run = overrides.pop("agent_run", _agent_run())
    assert isinstance(agent_run, AgentRunRequest)
    payload: dict[str, object] = {
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
    }
    payload.update(overrides)
    return TaskRequest.model_validate(payload)


class _ExactSecretPort:
    def __init__(self, authorized: dict[str, bytes]) -> None:
        self._authorized = dict(authorized)

    def resolve(self, handle: str) -> bytes:
        try:
            return bytes(self._authorized[handle])
        except KeyError as error:
            raise SecretHandleUnauthorized(f"unauthorized secret handle: {handle}") from error


def _context(
    tmp_path: Path | None = None,
    *,
    secrets: SecretPort | None = None,
) -> TaskContext:
    return TaskContext(
        workspace_root=(tmp_path or Path(".")).resolve(),
        heartbeat=lambda: None,
        cancel_requested=lambda: False,
        invocation=InvocationMetadata(
            invocation_id="inv-1",
            lock_digest=_SHA,
            composition_digest="b" * 64,
            entrypoint="runtime.cursor.execute",
        ),
        secrets=secrets if secrets is not None else _ExactSecretPort({"cursor.api-key": _CANARY}),
    )


async def test_cursor_launch_is_exact_and_shell_free(tmp_path: Path) -> None:
    host = FakeConfinedProcessHost()
    config = _config(tmp_path)
    await CursorHandler(config, host).execute(_request(), _context(tmp_path))
    launch = host.launches[0]
    assert launch.argv[:3] == (config.executable, "agent", "--print")
    assert launch.shell is False
    assert launch.cwd == tmp_path.resolve()
    assert set(launch.environment) == {"PATH", "CURSOR_API_KEY"}


async def test_launch_rejects_host_without_descendant_confinement(tmp_path: Path) -> None:
    with pytest.raises(TaskActivityProtocolViolation, match="confinement"):
        await CursorHandler(_config(tmp_path), FakeConfinedProcessHost(available=False)).execute(
            _request(), _context(tmp_path)
        )


async def test_pid_plus_finally_kill_fails_preflight(tmp_path: Path) -> None:
    host = FakeConfinedProcessHost(mechanism="pid-kill", descendant_inheritance=False)
    with pytest.raises(TaskActivityProtocolViolation, match="confinement"):
        await CursorHandler(_config(tmp_path), host).execute(_request(), _context(tmp_path))
    assert host.launches == []


async def test_launch_does_not_inherit_ambient_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CURSOR_API_KEY", "ambient-leak")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    host = FakeConfinedProcessHost()
    config = _config(tmp_path)
    await CursorHandler(config, host).execute(_request(), _context(tmp_path))
    launch = host.launches[0]
    assert launch.environment["CURSOR_API_KEY"] == _SECRET_TEXT
    assert launch.environment["CURSOR_API_KEY"] != "ambient-leak"
    assert launch.environment["PATH"] == str(Path(config.executable).parent)
    assert launch.environment["PATH"] != os.environ["PATH"]
    assert "HTTP_PROXY" not in launch.environment
    assert set(launch.environment) == {"PATH", "CURSOR_API_KEY"}


async def test_launch_keeps_secrets_out_of_argv_and_fingerprint(tmp_path: Path) -> None:
    host = FakeConfinedProcessHost()
    config = _config(tmp_path)
    handler = CursorHandler(config, host)
    await handler.execute(_request(), _context(tmp_path))
    launch = host.launches[0]
    assert _SECRET_TEXT not in launch.argv
    fingerprint_text = json.dumps(handler.dispatch_fingerprint, sort_keys=True)
    assert _SECRET_TEXT not in fingerprint_text
    assert "secret" not in fingerprint_text
    assert "canary" not in fingerprint_text
    assert handler.dispatch_fingerprint["protocol_profile"] == "confined_process"
    assert handler.dispatch_fingerprint["executable_digest"] == config.executable_digest
    assert handler.dispatch_fingerprint["argv_policy_digest"] == canonical_digest(
        {
            "argv": list(launch.argv),
            "cwd_policy": "attempt-workspace",
            "environment_names": ["CURSOR_API_KEY", "PATH"],
            "shell": False,
            "stdin": "canonical-agent-run-request",
        }
    )
    assert handler.dispatch_fingerprint["executable_version_digest"] == canonical_digest("1.0.0")
    assert launch.stdin == canonical_json_bytes(_agent_run().model_dump(mode="json"))
    assert "--resume" not in launch.argv
    assert launch.argv[3:5] == ("--output-format", "stream-json")


async def test_launch_rejects_digest_mismatch_before_spawn(tmp_path: Path) -> None:
    host = FakeConfinedProcessHost()
    config = _config(tmp_path, executable_digest="e" * 64)
    with pytest.raises(ValueError, match="digest"):
        await CursorHandler(config, host).execute(_request(), _context(tmp_path))
    assert host.launches == []


async def test_launch_rejects_relative_executable_search(tmp_path: Path) -> None:
    binary = _write_cursor_bin(tmp_path)
    with pytest.raises(ValidationError):
        _config(tmp_path, executable=Path(binary).name)


async def test_launch_rejects_unsupported_selection_fields(tmp_path: Path) -> None:
    dumped = _agent_run().model_dump(mode="json")
    dumped["execution"]["fallback_model"] = "auto"
    with pytest.raises(ValidationError):
        AgentRunRequest.model_validate(dumped)
    host = FakeConfinedProcessHost()
    with pytest.raises(ValidationError):
        await CursorHandler(_config(tmp_path), host).execute(
            _request(agent_run=_agent_run(), input={**dumped}),
            _context(tmp_path),
        )
    assert host.launches == []


async def test_launch_rejects_undeclared_secret_handle(tmp_path: Path) -> None:
    host = FakeConfinedProcessHost()
    with pytest.raises(SecretHandleUnauthorized):
        await CursorHandler(_config(tmp_path), host).execute(
            _request(),
            _context(tmp_path, secrets=_ExactSecretPort({})),
        )
    assert host.launches == []
