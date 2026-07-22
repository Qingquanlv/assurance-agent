"""Detached background launch for `aa workflow run --detach` / `aa workflow start`.

Acquire the lock, write driver.json, spawn a detached `aa workflow run ...
--adopt-lock <token>`, repoint lock + driver.pid at the child, and return
immediately so the caller is not blocked for the whole run. The child projects
the graph invocation ID into driver.json after start.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from assurance_agent.change_location import ChangeNotFoundError, resolve_change
from assurance_agent.config import ConfigNotFoundError
from assurance_agent.identifiers import UnsafeIdentifierError
from assurance_agent.workflow.driver.adapter import DriverError
from assurance_agent.workflow.driver.driver_state import (
    acquire_lock,
    create_initial_driver_state,
    evaluate_start_guard,
    release_lock,
    rewrite_lock_pid,
    write_driver_state,
)
from assurance_agent.workflow.driver.process_runner import resolve_aa_command

SpawnFn = Callable[[list[str], str, Path], int]


@dataclass
class StartResult:
    ok: bool
    message: str
    run_id: str | None = None
    pid: int | None = None


def resolve_opencode_server_url(
    server: str | None = None,
    env: Mapping[str, str] | None = None,
) -> str:
    env = env if env is not None else os.environ
    if server and server.strip():
        return server.strip().rstrip("/")
    from_env = env.get("AA_OPENCODE_SERVER_URL") or env.get("OPENCODE_SERVER_URL")
    if from_env and from_env.strip():
        return from_env.strip().rstrip("/")
    raise DriverError(
        "Cannot resolve OpenCode server URL. Set AA_OPENCODE_SERVER_URL "
        "(or OPENCODE_SERVER_URL), or pass --server."
    )


def _default_spawn(argv: list[str], cwd: str, log_path: Path) -> int:
    log_fd = open(log_path, "ab")  # noqa: SIM115 — kept open for the detached child
    try:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=log_fd,
            stderr=log_fd,
            start_new_session=True,
        )
    finally:
        log_fd.close()
    return proc.pid


def start_workflow_detached(
    *,
    project_root: Path,
    change_id: str,
    entrypoint: str,
    adapter: str = "opencode",
    params: dict | None = None,
    agent_cmd: str | None = None,
    model: str | None = None,
    parent_session: str | None = None,
    server: str | None = None,
    directory: str | None = None,
    spawn: SpawnFn | None = None,
    aa_command: list[str] | None = None,
) -> StartResult:
    params = params or {}
    try:
        change_dir = resolve_change(project_root, change_id).path
    except (UnsafeIdentifierError, ChangeNotFoundError, ConfigNotFoundError) as err:
        return StartResult(ok=False, message=str(err))
    directory = directory or str(project_root)

    guard = evaluate_start_guard(change_dir)
    if not guard.allowed:
        return StartResult(ok=False, message=guard.reason or "start refused")

    server_url: str | None = None
    if adapter == "opencode":
        try:
            server_url = resolve_opencode_server_url(server)
        except DriverError as err:
            return StartResult(ok=False, message=str(err))

    driver = create_initial_driver_state(
        directory=str(project_root),
        parent_session_id=parent_session,
        existing_run_id=guard.existing.run_id if guard.existing else None,
    )
    if guard.existing is not None:
        driver.invocation_id = guard.existing.invocation_id
        driver.checkpoint_id = guard.existing.checkpoint_id
        driver.event_seq = guard.existing.event_seq
    try:
        acquire_lock(change_dir, driver.start_token)
    except DriverError as err:
        return StartResult(ok=False, message=str(err))
    write_driver_state(change_dir, driver)

    aa = aa_command or resolve_aa_command()
    argv = [
        *aa,
        "workflow",
        "run",
        "--change",
        change_id,
        "--entrypoint",
        entrypoint,
        "--adapter",
        adapter,
        "--directory",
        directory,
    ]
    if adapter == "opencode":
        argv += ["--server", server_url or ""]
        if model:
            argv += ["--model", model]
    else:
        argv += ["--agent-cmd", agent_cmd or "cursor-agent --print"]
        # Headless/cursor-agent also honors --model (default applied in workflow_cmd).
        if model:
            argv += ["--model", model]
    if parent_session:
        argv += ["--parent-session", parent_session]
    if params:
        argv += ["--params", json.dumps(params)]
    argv += ["--adopt-lock", driver.start_token]

    change_dir.mkdir(parents=True, exist_ok=True)
    log_path = change_dir / "driver.log"
    spawn = spawn or _default_spawn
    try:
        pid = spawn(argv, directory, log_path)
    except Exception as err:  # noqa: BLE001 — release lock on any spawn failure
        release_lock(change_dir)
        return StartResult(ok=False, message=f"spawn failed: {err}")
    if not pid:
        release_lock(change_dir)
        return StartResult(ok=False, message="spawn failed: no pid")

    driver.pid = pid
    write_driver_state(change_dir, driver)
    rewrite_lock_pid(change_dir, pid, driver.start_token)
    return StartResult(
        ok=True,
        run_id=driver.run_id,
        pid=pid,
        message=(
            f"workflow started (run_id={driver.run_id}, entrypoint={entrypoint}); "
            f"log: qa/changes/{change_id}/driver.log"
        ),
    )
