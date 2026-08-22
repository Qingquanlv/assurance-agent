from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest
from agent_runtime_contracts.schema import canonical_digest, canonical_json_bytes
from agent_runtime_cursor.process import (
    LinuxProcessSupervisorHost,
    MacOSProcessGroupHost,
    ProcessLaunchRequest,
    argv_policy_document,
    production_process_host,
)
from graph_engine import TaskActivityProtocolViolation

from test_process_host import launch_request  # noqa: PLC2701


async def test_spawn_rejects_symlink_executable(tmp_path: Path) -> None:
    real = tmp_path / "real-exe"
    real.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    real.chmod(0o755)
    link = tmp_path / "exe-link"
    link.symlink_to(real)
    env = {"PATH": str(tmp_path)}
    argv = (str(link), "-c", "print('ok')")
    policy = argv_policy_document(argv, ("PATH",))
    request = ProcessLaunchRequest(
        argv=argv,
        cwd=tmp_path.resolve(),
        environment=env,
        stdin=b"",
        shell=False,
        executable_version_digest=canonical_digest("1.0.0"),
        request_digest=canonical_digest({"test": "symlink"}),
        argv_policy_digest=canonical_digest(policy),
        workspace_identity_digest=canonical_digest({"cwd": str(tmp_path.resolve())}),
    )
    host = production_process_host(tmp_path)
    with pytest.raises(TaskActivityProtocolViolation, match="regular file"):
        await host.spawn(request)


async def test_spawn_rejects_ambient_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    host = production_process_host(tmp_path)
    captured: dict[str, object] = {}

    class _RecordingPopen:
        def __init__(self, *args: object, **kwargs: object) -> None:
            captured["args"] = args
            captured["kwargs"] = kwargs
            self.stdin = None
            self.stdout = None
            self.stderr = None
            self.pid = 4242
            self.returncode = None

        def __enter__(self) -> _RecordingPopen:
            return self

        def __exit__(self, *args: object) -> bool:
            return False

        def poll(self) -> int | None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            return 0

        def communicate(self, input: bytes | None = None, timeout: float | None = None) -> tuple[bytes, bytes]:
            del input, timeout
            return b"", b""

    monkeypatch.setattr("agent_runtime_cursor.process.subprocess.Popen", _RecordingPopen)
    monkeypatch.setattr(host, "_read_start_identity", lambda pid: f"proc:{pid}:start")
    monkeypatch.setenv("CURSOR_SHOULD_NOT_LEAK", "secret-value")
    request = launch_request(tmp_path, environment={"PATH": "/usr/bin"})
    await host.spawn(request)
    env = captured["kwargs"]["env"]  # type: ignore[index]
    assert isinstance(env, dict)
    assert set(env) <= {"PATH", "PYTHONUNBUFFERED", "HOME"}
    assert env["HOME"] == str(tmp_path.resolve())
    assert "CURSOR_SHOULD_NOT_LEAK" not in env
    assert set(request.environment) == {"PATH"}


async def test_spawn_rejects_shell_execution(tmp_path: Path) -> None:
    host = production_process_host(tmp_path)
    request = launch_request(tmp_path, shell=True)
    with pytest.raises(TaskActivityProtocolViolation, match="shell"):
        await host.spawn(request)


async def test_spawn_rejects_environment_name_drift(tmp_path: Path) -> None:
    host = production_process_host(tmp_path)
    request = launch_request(tmp_path, environment={"PATH": "/usr/bin", "HOME": "/tmp"})
    drifted_policy = argv_policy_document(request.argv, ("PATH",))
    request = ProcessLaunchRequest(
        argv=request.argv,
        cwd=request.cwd,
        environment=request.environment,
        stdin=request.stdin,
        shell=request.shell,
        executable_version_digest=request.executable_version_digest,
        request_digest=request.request_digest,
        argv_policy_digest=canonical_digest(drifted_policy),
        workspace_identity_digest=request.workspace_identity_digest,
    )
    with pytest.raises(TaskActivityProtocolViolation, match="argv policy is not authentic"):
        await host.spawn(request)


async def test_spawn_rejects_workspace_identity_drift(tmp_path: Path) -> None:
    host = production_process_host(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    request = launch_request(tmp_path, cwd=other)
    request = ProcessLaunchRequest(
        argv=request.argv,
        cwd=request.cwd,
        environment=request.environment,
        stdin=request.stdin,
        shell=request.shell,
        executable_version_digest=request.executable_version_digest,
        request_digest=request.request_digest,
        argv_policy_digest=request.argv_policy_digest,
        workspace_identity_digest=canonical_digest({"cwd": str(tmp_path.resolve())}),
    )
    with pytest.raises(TaskActivityProtocolViolation, match="workspace identity drifted"):
        await host.spawn(request)


def test_macos_spawn_uses_new_process_group(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    host = MacOSProcessGroupHost(tmp_path)
    captured: dict[str, object] = {}

    class _RecordingPopen:
        def __init__(self, *args: object, **kwargs: object) -> None:
            captured["kwargs"] = kwargs
            self.stdin = None
            self.stdout = None
            self.stderr = None
            self.pid = 5151
            self.returncode = None

        def __enter__(self) -> _RecordingPopen:
            return self

        def __exit__(self, *args: object) -> bool:
            return False

        def poll(self) -> int | None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            return 0

        def communicate(self, input: bytes | None = None, timeout: float | None = None) -> tuple[bytes, bytes]:
            del input, timeout
            return b"ok\n", b""

    monkeypatch.setattr("agent_runtime_cursor.process.subprocess.Popen", _RecordingPopen)
    monkeypatch.setattr(host, "_read_start_identity", lambda pid: f"proc:{pid}:1:1")
    import asyncio

    asyncio.run(host.spawn(launch_request(tmp_path)))
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs.get("start_new_session") is True
    assert kwargs.get("shell") is False
    env = kwargs["env"]
    assert isinstance(env, dict)
    assert set(env) <= {"PATH", "PYTHONUNBUFFERED", "HOME"}
    assert env["HOME"] == str(tmp_path.resolve())


def test_linux_spawn_records_supervised_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    host = LinuxProcessSupervisorHost(tmp_path)
    captured: dict[str, object] = {}

    class _RecordingPopen:
        def __init__(self, *args: object, **kwargs: object) -> None:
            captured["kwargs"] = kwargs
            self.stdin = None
            self.stdout = None
            self.stderr = None
            self.pid = 6161
            self.returncode = None

        def poll(self) -> int | None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            return 0

        def communicate(self, input: bytes | None = None, timeout: float | None = None) -> tuple[bytes, bytes]:
            del input, timeout
            return b"ok\n", b""

    monkeypatch.setattr("agent_runtime_cursor.process.subprocess.Popen", _RecordingPopen)
    monkeypatch.setattr(
        host,
        "_read_proc_stat",
        lambda pid: f"{pid} (sleep) S 1 {pid} {pid} 0 0 1 0 0 0 0 0 20 0 1 0 6161 4096 64 777777 0 0 0 0 0 0 0 0 0 0 0",
    )
    import asyncio

    process = asyncio.run(host.spawn(launch_request(tmp_path)))
    assert process.receipt.confinement_identity.startswith("session:")
    assert canonical_json_bytes({"pid": 6161})  # keep import used
