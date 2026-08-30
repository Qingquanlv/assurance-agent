from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from agent_runtime_contracts.schema import canonical_digest
from agent_runtime_cursor.filesystem_sandbox import FilesystemSandbox
from agent_runtime_cursor.process import (
    LinuxProcessSupervisorHost,
    MacOSProcessGroupHost,
    ProcessLaunchRequest,
    argv_policy_document,
    production_process_host,
)


_WRITE_ROOT = "qa/changes/CH-1/.staging/task-1/attempt-1"


def _launch(
    tmp_path: Path,
    *,
    argv: tuple[str, ...] | None = None,
    environment: dict[str, str] | None = None,
) -> ProcessLaunchRequest:
    project = (tmp_path / "project").resolve()
    project.mkdir(exist_ok=True)
    write_root = (project / _WRITE_ROOT).resolve()
    write_root.mkdir(parents=True, exist_ok=True)
    executable = str(Path(sys.executable).resolve())
    command = argv or (executable, "-c", "print('ok')")
    env = environment or {"PATH": os.environ.get("PATH", "/usr/bin")}
    policy = argv_policy_document(command, tuple(sorted(env)))
    return ProcessLaunchRequest(
        argv=command,
        cwd=project,
        write_root=write_root,
        environment=env,
        stdin=b"",
        shell=False,
        executable_version_digest=canonical_digest("1.0.0"),
        request_digest=canonical_digest({"test": "sandbox"}),
        argv_policy_digest=canonical_digest(policy),
        workspace_identity_digest=canonical_digest(
            {"project_root": str(project), "write_root": str(write_root)}
        ),
    )


def test_wrap_fails_closed_when_sandbox_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(ValueError, match="unavailable"):
        FilesystemSandbox.wrap(_launch(tmp_path))


def test_wrap_authenticates_sandbox_executable_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_runtime_cursor.filesystem_sandbox import FilesystemSandbox as LiveSandbox
    import agent_runtime_cursor.filesystem_sandbox as sandbox_mod

    fake = tmp_path / "sandbox-exec"
    fake.write_bytes(b"not-sandbox")
    fake.chmod(0o755)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(sandbox_mod, "SANDBOX_EXEC", fake)
    wrapped = LiveSandbox.wrap(_launch(tmp_path))
    assert Path(wrapped.argv[0]).resolve() == fake.resolve()
    assert not Path(wrapped.argv[0]).is_symlink()
    assert stat.S_ISREG(Path(wrapped.argv[0]).stat().st_mode)
    assert (
        wrapped.sandbox_profile_digest == hashlib.sha256(wrapped.sandbox_profile.encode("utf-8")).hexdigest()
    )


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS Seatbelt profile")
def test_macos_wrap_uses_sandbox_exec_with_attempt_and_temp_writes_only(tmp_path: Path) -> None:
    request = _launch(tmp_path)
    wrapped = FilesystemSandbox.wrap(request)
    assert Path(wrapped.argv[0]).name == "sandbox-exec"
    assert Path(wrapped.argv[0]).is_file()
    assert not Path(wrapped.argv[0]).is_symlink()
    assert "-p" in wrapped.argv
    profile = wrapped.sandbox_profile
    assert "(version 1)" in profile
    assert f'(subpath "{request.cwd}")' in profile or str(request.cwd) in profile
    assert str(request.write_root) in profile
    assert wrapped.temp_root is not None
    assert str(wrapped.temp_root) in profile
    assert profile.count("(allow file-write*") >= 1
    assert "deny default" in profile
    assert wrapped.sandbox_profile_digest == hashlib.sha256(profile.encode("utf-8")).hexdigest()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux bubblewrap")
def test_linux_wrap_uses_authenticated_bwrap_readonly_root(tmp_path: Path) -> None:
    request = _launch(tmp_path)
    wrapped = FilesystemSandbox.wrap(request)
    assert Path(wrapped.argv[0]).name == "bwrap"
    assert Path(wrapped.argv[0]).is_file()
    assert not Path(wrapped.argv[0]).is_symlink()
    assert "--ro-bind" in wrapped.argv
    assert wrapped.argv[wrapped.argv.index("--ro-bind") : wrapped.argv.index("--ro-bind") + 3] == (
        "--ro-bind",
        "/",
        "/",
    )
    assert str(request.write_root) in wrapped.argv
    assert wrapped.temp_root is not None
    assert str(wrapped.temp_root) in wrapped.argv
    assert wrapped.sandbox_profile_digest


def test_production_spawn_records_sandbox_profile_digest(tmp_path: Path) -> None:
    host = production_process_host(tmp_path / "host")
    assert isinstance(host, MacOSProcessGroupHost | LinuxProcessSupervisorHost)
    request = _launch(tmp_path)
    try:
        import asyncio

        process = asyncio.run(host.spawn(request))
        assert process.receipt.sandbox_profile_digest
        assert len(process.receipt.sandbox_profile_digest) == 64
        terminal = asyncio.run(host.wait(process.receipt))
        assert terminal.exit_code == 0
    finally:
        host.close()


def _probe_script(project: Path, write_root: Path, target: Path, action: str) -> str:
    return (
        "import os, pathlib, sys\n"
        f"project = pathlib.Path({str(project)!r})\n"
        f"write_root = pathlib.Path({str(write_root)!r})\n"
        f"target = pathlib.Path({str(target)!r})\n"
        f"action = {action!r}\n"
        "try:\n"
        "    if action == 'write':\n"
        "        target.write_text('hijack\\n', encoding='utf-8')\n"
        "    elif action == 'rename':\n"
        "        os.rename(target, str(target) + '.bak')\n"
        "    elif action == 'replace':\n"
        "        os.replace(target, str(target) + '.replaced')\n"
        "    elif action == 'swap':\n"
        "        swapped = target.with_name(target.name + '.swap')\n"
        "        os.rename(target, swapped)\n"
        "        os.rename(swapped, target)\n"
        "    print('SUCCEEDED')\n"
        "except OSError as error:\n"
        "    print(f'DENIED:{error.errno}')\n"
        "    sys.exit(2)\n"
    )


def _run_sandboxed(tmp_path: Path, target: Path, action: str) -> subprocess.CompletedProcess[str]:
    request = _launch(tmp_path)
    script = _probe_script(request.cwd, request.write_root, target, action)
    probe = _launch(tmp_path, argv=(str(Path(sys.executable).resolve()), "-c", script))
    wrapped = FilesystemSandbox.wrap(probe)
    env = dict(wrapped.environment)
    return subprocess.run(
        list(wrapped.argv),
        check=False,
        cwd=str(wrapped.cwd),
        env=env,
        capture_output=True,
        text=True,
    )


@pytest.mark.skipif(sys.platform != "darwin" and not sys.platform.startswith("linux"), reason="OS sandbox")
def test_sandbox_allows_only_authenticated_attempt_write_root(tmp_path: Path) -> None:
    request = _launch(tmp_path)
    allowed = request.write_root / "ok.txt"
    result = _run_sandboxed(tmp_path, allowed, "write")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "SUCCEEDED" in result.stdout
    assert allowed.read_text(encoding="utf-8") == "hijack\n"


@pytest.mark.skipif(sys.platform != "darwin" and not sys.platform.startswith("linux"), reason="OS sandbox")
@pytest.mark.parametrize("action", ["write", "rename", "replace", "swap"])
def test_sandbox_denies_rename_replace_and_swap_of_project_root_and_output_parent(
    tmp_path: Path, action: str
) -> None:
    request = _launch(tmp_path)
    output_parent = request.cwd / "qa/changes/CH-1"
    output_parent.mkdir(parents=True, exist_ok=True)
    (output_parent / "proposal.md").write_text("keep\n", encoding="utf-8")
    for target in (request.cwd, output_parent):
        result = _run_sandboxed(tmp_path, target, action)
        assert result.returncode != 0
        assert "SUCCEEDED" not in result.stdout
        assert request.cwd.is_dir()
        assert output_parent.is_dir()
        assert (output_parent / "proposal.md").read_text(encoding="utf-8") == "keep\n"
        assert not Path(str(request.cwd) + ".bak").exists()
        assert not Path(str(output_parent) + ".bak").exists()
