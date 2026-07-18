"""Driver lock + driver.json state file + start/resume guard (spec 5a).

Clean-room port of the TS driver_state.ts semantics: an O_EXCL lock whose stale
copies (dead pid, or start_token diverged from driver.json) are auto-reclaimed;
an atomic driver.json holding progress for `aa workflow status`; and an
idempotent start guard (running+alive → refuse duplicate; completed → refuse
restart; paused/failed/running-with-dead-pid → allow resume).
"""

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel

from assurance_agent.workflow.driver.adapter import DriverError

DriverStatus = Literal["running", "paused", "completed", "failed"]


class DriverState(BaseModel):
    run_id: str
    status: DriverStatus
    pid: int
    start_token: str
    started_at: str
    updated_at: str
    parent_session_id: str | None = None
    directory: str
    current_phase: str | None = None
    current_attempt_id: str | None = None
    paused_on: str | None = None
    # Checkpoint 语义：主循环每提交一个控制动作或相位结果即过一个迭代边界，
    # iteration 随之递增；checkpoint 实体 = workflow-state.yaml + events.jsonl
    # （纯投影可据此恢复），driver.json 只是 checkpoint 指针。
    iteration: int = 0
    last_checkpoint_at: str | None = None


class StartGuard(BaseModel):
    allowed: bool
    reason: str | None = None
    existing: DriverState | None = None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def driver_json_path(change_dir: Path) -> Path:
    return change_dir / "driver.json"


def driver_lock_path(change_dir: Path) -> Path:
    return change_dir / "driver.lock"


def read_driver_state(change_dir: Path) -> DriverState | None:
    path = driver_json_path(change_dir)
    if not path.is_file():
        return None
    return DriverState.model_validate_json(path.read_text(encoding="utf-8"))


def write_driver_state(change_dir: Path, state: DriverState) -> None:
    change_dir.mkdir(parents=True, exist_ok=True)
    path = driver_json_path(change_dir)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(state.model_dump_json(indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def create_initial_driver_state(
    directory: str,
    parent_session_id: str | None = None,
    existing_run_id: str | None = None,
) -> DriverState:
    now = now_iso()
    return DriverState(
        run_id=existing_run_id or str(uuid4()),
        status="running",
        pid=os.getpid(),
        start_token=str(uuid4()),
        started_at=now,
        updated_at=now,
        parent_session_id=parent_session_id,
        directory=directory,
    )


def _write_lock(lock: Path, start_token: str) -> None:
    with open(lock, "x", encoding="utf-8") as handle:
        handle.write(f"{os.getpid()}\n{start_token}\n")


def acquire_lock(change_dir: Path, start_token: str) -> None:
    change_dir.mkdir(parents=True, exist_ok=True)
    lock = driver_lock_path(change_dir)
    try:
        _write_lock(lock, start_token)
        return
    except FileExistsError:
        pass

    lock_pid = 0
    lock_token = ""
    try:
        lines = lock.read_text(encoding="utf-8").strip().split("\n")
        lock_pid = int(lines[0])
        lock_token = lines[1] if len(lines) > 1 else ""
    except (OSError, ValueError):
        pass

    existing = read_driver_state(change_dir)
    pid_dead = not is_pid_alive(lock_pid)
    token_mismatch = existing is not None and existing.start_token != lock_token

    if pid_dead or token_mismatch:
        lock.unlink(missing_ok=True)
        _write_lock(lock, start_token)
        return

    raise DriverError(
        f"driver.lock held by live pid {lock_pid} (token={lock_token or 'unknown'}); refuse duplicate start"
    )


def release_lock(change_dir: Path) -> None:
    driver_lock_path(change_dir).unlink(missing_ok=True)


def rewrite_lock_pid(change_dir: Path, pid: int, start_token: str) -> None:
    driver_lock_path(change_dir).write_text(f"{pid}\n{start_token}\n", encoding="utf-8")


def evaluate_start_guard(change_dir: Path) -> StartGuard:
    existing = read_driver_state(change_dir)
    if existing is None:
        return StartGuard(allowed=True)
    if existing.status == "running" and is_pid_alive(existing.pid):
        return StartGuard(
            allowed=False,
            reason=f"driver already running (pid {existing.pid}, run_id {existing.run_id})",
            existing=existing,
        )
    if existing.status == "completed":
        return StartGuard(
            allowed=False,
            reason=f"driver already completed (run_id {existing.run_id}); refuse restart",
            existing=existing,
        )
    return StartGuard(allowed=True, existing=existing)
