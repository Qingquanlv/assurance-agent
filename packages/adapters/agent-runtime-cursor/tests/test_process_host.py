from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import pytest
from agent_runtime_contracts.schema import canonical_digest
from agent_runtime_cursor.process import (
    CancelPolicy,
    ProcessLaunchRequest,
    UnsupportedCursorPlatform,
    argv_policy_document,
    production_process_host,
)
from graph_engine import TaskActivityProtocolViolation


def launch_request(
    tmp_path: Path,
    *,
    argv: tuple[str, ...] | None = None,
    environment: dict[str, str] | None = None,
    shell: bool = False,
    cwd: Path | None = None,
) -> ProcessLaunchRequest:
    resolved = (cwd or tmp_path).resolve()
    write_root = (resolved / "stage").resolve()
    write_root.mkdir(exist_ok=True)
    executable = str(Path(sys.executable).resolve())
    command = argv or (executable, "-c", "print('ok')")
    if argv is not None and argv:
        command = (str(Path(argv[0]).resolve()), *argv[1:])
    env_names = tuple(sorted((environment or {"PATH": os.environ.get("PATH", "/usr/bin")}).keys()))
    policy = argv_policy_document(command, env_names)
    return ProcessLaunchRequest(
        argv=command,
        cwd=resolved,
        write_root=write_root,
        environment=environment or {"PATH": os.environ.get("PATH", "/usr/bin")},
        stdin=b"",
        shell=shell,
        executable_version_digest=canonical_digest("1.0.0"),
        request_digest=canonical_digest({"test": "launch"}),
        argv_policy_digest=canonical_digest(policy),
        workspace_identity_digest=canonical_digest(
            {"project_root": str(resolved), "write_root": str(write_root)}
        ),
    )


def spawning_child_request(tmp_path: Path) -> ProcessLaunchRequest:
    executable = str(Path(sys.executable).resolve())
    script = (
        "import subprocess, sys, time\n"
        f"subprocess.Popen([{executable!r}, '-c', 'import time; time.sleep(120)'], "
        "start_new_session=True)\n"
        "time.sleep(120)\n"
    )
    return launch_request(tmp_path, argv=(executable, "-c", script))


async def no_recorded_pid_survives(receipt: object, *, host_root: Path) -> bool:
    from agent_runtime_cursor.process import CursorProcessReceipt, production_process_host

    payload = receipt.model_dump() if hasattr(receipt, "model_dump") else receipt
    bound = CursorProcessReceipt.model_validate(payload)
    host = production_process_host(host_root)
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        try:
            host.authenticate(bound)
            observation = await host.observe(bound)
            if observation.status == "running":
                await asyncio.sleep(0.05)
                continue
        except Exception:
            return True
        return observation.status != "running"
    return False


async def test_real_host_records_terminal_before_return(tmp_path: Path) -> None:
    host = production_process_host(tmp_path)
    request = launch_request(tmp_path)
    process = await host.spawn(request)
    terminal = await host.wait(process.receipt)
    assert terminal.exit_code == 0
    assert host.read_durable_terminal(process.receipt) == terminal


async def test_cancel_terminates_the_entire_process_group(tmp_path: Path) -> None:
    host = production_process_host(tmp_path)
    process = await host.spawn(spawning_child_request(tmp_path))
    await host.terminate(
        process.receipt,
        CancelPolicy(graceful_seconds=0.1, forced_seconds=0.1),
    )
    await asyncio.sleep(0.2)
    assert await no_recorded_pid_survives(process.receipt, host_root=tmp_path)


async def test_observe_running_process(tmp_path: Path) -> None:
    host = production_process_host(tmp_path)
    request = launch_request(
        tmp_path,
        argv=(sys.executable, "-c", "import time; time.sleep(2)"),
    )
    process = await host.spawn(request)
    observation = await host.observe(process.receipt)
    assert observation.status == "running"
    assert observation.exit_code is None
    terminal = await host.wait(process.receipt)
    assert terminal.exit_code == 0


async def test_preflight_rejects_shell(tmp_path: Path) -> None:
    host = production_process_host(tmp_path)
    request = launch_request(tmp_path, shell=True)
    with pytest.raises(TaskActivityProtocolViolation, match="shell"):
        host.preflight(request)


def test_production_process_host_rejects_windows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(UnsupportedCursorPlatform, match="Linux and macOS"):
        production_process_host(tmp_path)


async def test_wait_is_idempotent_from_durable_terminal(tmp_path: Path) -> None:
    host = production_process_host(tmp_path)
    process = await host.spawn(launch_request(tmp_path))
    first = await host.wait(process.receipt)
    second = await host.wait(process.receipt)
    assert first == second
    assert host.read_durable_terminal(process.receipt) == first


async def test_durable_terminal_survives_new_host_instance(tmp_path: Path) -> None:
    host = production_process_host(tmp_path)
    process = await host.spawn(launch_request(tmp_path))
    terminal = await host.wait(process.receipt)
    restarted = production_process_host(tmp_path)
    restarted.authenticate(process.receipt)
    assert restarted.read_durable_terminal(process.receipt) == terminal


async def test_progress_heartbeat_advances_during_long_wait(tmp_path: Path) -> None:
    host = production_process_host(tmp_path)
    request = launch_request(
        tmp_path,
        argv=(sys.executable, "-c", "import time; time.sleep(2)"),
    )
    process = await host.spawn(request)
    initial = host.read_progress_heartbeat(process.receipt)
    assert initial is not None

    wait_task = asyncio.create_task(host.wait(process.receipt))
    try:
        deadline = time.monotonic() + 1.5
        advanced = False
        while time.monotonic() < deadline:
            await asyncio.sleep(0.15)
            current = host.read_progress_heartbeat(process.receipt)
            assert current is not None
            if current > initial + 0.05:
                advanced = True
                break
        assert advanced
    finally:
        await wait_task


async def test_spawn_home_using_wrapper_succeeds_with_cwd_injection(tmp_path: Path) -> None:
    wrapper = tmp_path / "home-wrapper.sh"
    wrapper.write_text(
        '#!/usr/bin/env bash\nset -u\n: "${HOME:?HOME required}"\nexec "$1" "${@:2}"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    host = production_process_host(tmp_path)
    request = launch_request(
        tmp_path,
        argv=(str(wrapper), sys.executable, "-c", "print('ok')"),
        environment={"PATH": os.environ.get("PATH", "/usr/bin")},
    )
    assert set(request.environment.keys()) == {"PATH"}
    process = await host.spawn(request)
    terminal = await host.wait(process.receipt)
    assert terminal.exit_code == 0
    assert b"ok" in terminal.stdout


async def test_wait_drains_stderr_concurrently_without_deadlock(tmp_path: Path) -> None:
    host = production_process_host(tmp_path)
    script = (
        "import sys\n"
        "for _ in range(500):\n"
        "    sys.stderr.write('x' * 1000)\n"
        "    sys.stderr.flush()\n"
        "print('done')\n"
    )
    process = await host.spawn(launch_request(tmp_path, argv=(sys.executable, "-c", script)))
    terminal = await asyncio.wait_for(host.wait(process.receipt), timeout=10.0)
    assert terminal.exit_code == 0
    assert b"done" in terminal.stdout
    assert len(terminal.stderr) > 0


async def test_close_reaps_abandoned_child_group(tmp_path: Path) -> None:
    host = production_process_host(tmp_path)
    process = await host.spawn(spawning_child_request(tmp_path))
    host.close()
    await asyncio.sleep(0.3)
    assert await no_recorded_pid_survives(process.receipt, host_root=tmp_path)
