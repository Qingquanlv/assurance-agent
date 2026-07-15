import os
from pathlib import Path

import pytest

from assurance_agent.workflow.driver.adapter import DriverError
from assurance_agent.workflow.driver.driver_state import (
    DriverState,
    acquire_lock,
    create_initial_driver_state,
    driver_lock_path,
    evaluate_start_guard,
    is_pid_alive,
    read_driver_state,
    release_lock,
    write_driver_state,
)


def _write_state(change_dir: Path, **overrides: object) -> DriverState:
    state = create_initial_driver_state(directory=str(change_dir))
    for key, value in overrides.items():
        setattr(state, key, value)
    write_driver_state(change_dir, state)
    return state


def test_write_then_read_roundtrip(tmp_path: Path) -> None:
    written = _write_state(tmp_path, current_phase="explore")
    read = read_driver_state(tmp_path)
    assert read is not None
    assert read.run_id == written.run_id
    assert read.current_phase == "explore"
    assert read.status == "running"


def test_read_missing_returns_none(tmp_path: Path) -> None:
    assert read_driver_state(tmp_path) is None


def test_is_pid_alive_self_true_and_bogus_false() -> None:
    assert is_pid_alive(os.getpid()) is True
    assert is_pid_alive(-1) is False
    assert is_pid_alive(2_000_000_000) is False


def test_acquire_lock_then_second_fails_while_pid_alive(tmp_path: Path) -> None:
    state = _write_state(tmp_path)
    acquire_lock(tmp_path, state.start_token)
    # driver.json pid == this live process → live holder, refuse duplicate.
    with pytest.raises(DriverError):
        acquire_lock(tmp_path, "other-token")


def test_stale_lock_dead_pid_reclaimed(tmp_path: Path) -> None:
    change_dir = tmp_path
    change_dir.mkdir(exist_ok=True)
    # Lock left by a dead pid.
    driver_lock_path(change_dir).write_text("2000000000\nold-token\n", encoding="utf-8")
    _write_state(change_dir, pid=2_000_000_000, start_token="old-token")
    acquire_lock(change_dir, "new-token")  # must reclaim, not raise
    assert driver_lock_path(change_dir).read_text().splitlines()[1] == "new-token"


def test_stale_lock_token_mismatch_reclaimed(tmp_path: Path) -> None:
    # Live pid in the lock, but driver.json start_token no longer matches → stale.
    driver_lock_path(tmp_path).write_text(f"{os.getpid()}\norphan-token\n", encoding="utf-8")
    _write_state(tmp_path, pid=os.getpid(), start_token="current-token")
    acquire_lock(tmp_path, "fresh-token")
    assert driver_lock_path(tmp_path).read_text().splitlines()[1] == "fresh-token"


def test_release_lock_idempotent(tmp_path: Path) -> None:
    state = _write_state(tmp_path)
    acquire_lock(tmp_path, state.start_token)
    release_lock(tmp_path)
    assert not driver_lock_path(tmp_path).exists()
    release_lock(tmp_path)  # no error on missing lock


def test_start_guard_fresh_allowed(tmp_path: Path) -> None:
    assert evaluate_start_guard(tmp_path).allowed is True


def test_start_guard_running_alive_refused(tmp_path: Path) -> None:
    _write_state(tmp_path, status="running", pid=os.getpid())
    guard = evaluate_start_guard(tmp_path)
    assert guard.allowed is False
    assert guard.reason is not None and "already running" in guard.reason


def test_start_guard_completed_refused(tmp_path: Path) -> None:
    _write_state(tmp_path, status="completed")
    assert evaluate_start_guard(tmp_path).allowed is False


def test_start_guard_paused_allows_resume(tmp_path: Path) -> None:
    saved = _write_state(tmp_path, status="paused", paused_on="case-review")
    guard = evaluate_start_guard(tmp_path)
    assert guard.allowed is True
    assert guard.existing is not None and guard.existing.run_id == saved.run_id


def test_start_guard_running_dead_pid_allows_resume(tmp_path: Path) -> None:
    _write_state(tmp_path, status="running", pid=2_000_000_000)
    assert evaluate_start_guard(tmp_path).allowed is True
