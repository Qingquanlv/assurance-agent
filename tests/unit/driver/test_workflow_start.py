"""Detached workflow start uses --entrypoint and adopts the lock token."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.helpers_aa import write_aa_config

from assurance_agent.workflow.driver.adapter import DriverError
from assurance_agent.workflow.driver.driver_state import (
    create_initial_driver_state,
    driver_lock_path,
    read_driver_state,
    write_driver_state,
)
from assurance_agent.workflow.driver.workflow_start import (
    SpawnFn,
    resolve_opencode_server_url,
    start_workflow_detached,
)


def _spy_spawn(recorder: list) -> SpawnFn:
    def spawn(argv: list[str], cwd: str, log_path: Path) -> int:
        recorder.append({"argv": argv, "cwd": cwd, "log": str(log_path)})
        return 4242

    return spawn


def test_resolve_server_url_from_arg_env_and_error() -> None:
    assert resolve_opencode_server_url("http://h:1/", {}) == "http://h:1"
    assert resolve_opencode_server_url(None, {"AA_OPENCODE_SERVER_URL": "http://a:2"}) == "http://a:2"
    assert resolve_opencode_server_url(None, {"OPENCODE_SERVER_URL": "http://b:3"}) == "http://b:3"
    with pytest.raises(DriverError):
        resolve_opencode_server_url(None, {})


def test_detached_spawn_writes_driver_and_lock(tmp_path: Path) -> None:
    write_aa_config(tmp_path)
    (tmp_path / "qa" / "changes" / "CH-1").mkdir(parents=True)
    calls: list = []
    result = start_workflow_detached(
        project_root=tmp_path,
        change_id="CH-1",
        entrypoint="full",
        adapter="opencode",
        server="http://host:9",
        spawn=_spy_spawn(calls),
        aa_command=["aa"],
    )
    assert result.ok is True
    assert result.pid == 4242
    change_dir = tmp_path / "qa" / "changes" / "CH-1"
    driver = read_driver_state(change_dir)
    assert driver is not None and driver.pid == 4242
    lock = driver_lock_path(change_dir).read_text().splitlines()
    assert lock[0] == "4242" and lock[1] == driver.start_token
    assert "--entrypoint" in calls[0]["argv"]
    assert "full" in calls[0]["argv"]
    assert "--scope" not in calls[0]["argv"]


def test_detached_argv_for_opencode(tmp_path: Path) -> None:
    write_aa_config(tmp_path)
    (tmp_path / "qa" / "changes" / "CH-1").mkdir(parents=True, exist_ok=True)
    calls: list = []
    start_workflow_detached(
        project_root=tmp_path,
        change_id="CH-1",
        entrypoint="execute",
        adapter="opencode",
        server="http://host:9",
        model="prov/mod",
        parent_session="ses_1",
        params={"max_healing_attempts": 2},
        spawn=_spy_spawn(calls),
        aa_command=["aa"],
    )
    argv = calls[0]["argv"]
    assert argv[:2] == ["aa", "workflow"]
    assert "run" in argv
    assert "--entrypoint" in argv and "execute" in argv
    assert "--server" in argv and "http://host:9" in argv
    assert "--model" in argv and "prov/mod" in argv
    assert "--parent-session" in argv and "ses_1" in argv
    assert "--params" in argv
    assert "--adopt-lock" in argv


def test_detached_argv_for_headless(tmp_path: Path) -> None:
    write_aa_config(tmp_path)
    (tmp_path / "qa" / "changes" / "CH-1").mkdir(parents=True, exist_ok=True)
    calls: list = []
    start_workflow_detached(
        project_root=tmp_path,
        change_id="CH-1",
        entrypoint="execute",
        adapter="headless",
        agent_cmd="cursor-agent --print",
        spawn=_spy_spawn(calls),
        aa_command=["aa"],
    )
    argv = calls[0]["argv"]
    assert "--agent-cmd" in argv and "cursor-agent --print" in argv
    assert "--server" not in argv


def test_spawn_failure_releases_lock(tmp_path: Path) -> None:
    write_aa_config(tmp_path)
    (tmp_path / "qa" / "changes" / "CH-1").mkdir(parents=True, exist_ok=True)

    def bad_spawn(argv: list[str], cwd: str, log_path: Path) -> int:
        raise OSError("no fork")

    result = start_workflow_detached(
        project_root=tmp_path,
        change_id="CH-1",
        entrypoint="execute",
        adapter="headless",
        spawn=bad_spawn,
        aa_command=["aa"],
    )
    assert result.ok is False
    assert "spawn failed" in result.message
    assert not driver_lock_path(tmp_path / "qa" / "changes" / "CH-1").exists()


def test_start_guard_refuses_when_running(tmp_path: Path) -> None:
    write_aa_config(tmp_path)
    (tmp_path / "qa" / "changes" / "CH-1").mkdir(parents=True, exist_ok=True)
    import os

    change_dir = tmp_path / "qa" / "changes" / "CH-1"
    running = create_initial_driver_state(directory=str(tmp_path))
    running.status = "running"
    running.pid = os.getpid()
    write_driver_state(change_dir, running)

    result = start_workflow_detached(
        project_root=tmp_path,
        change_id="CH-1",
        entrypoint="execute",
        adapter="headless",
        spawn=_spy_spawn([]),
        aa_command=["aa"],
    )
    assert result.ok is False
    assert "already running" in result.message
