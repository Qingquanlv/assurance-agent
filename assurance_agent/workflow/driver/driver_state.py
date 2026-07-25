"""Driver lock + driver.json process pointer (non-authoritative).

``driver.json`` records the live OS process and the latest known graph pointer
(``invocation_id`` / ``checkpoint_id`` / ``event_seq``). Ledger projection via
``CheckpointStore`` is authoritative for task completion; a stale or absent
driver file never causes succeeded tasks to re-run.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel

from assurance_agent.workflow.driver.adapter import DriverError
from assurance_agent.workflow.graph.checkpoint import CheckpointStore

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
    invocation_id: str | None = None
    checkpoint_id: str | None = None
    event_seq: int = 0


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


def project_graph_pointer(
    state: DriverState,
    *,
    invocation_id: str | None,
    checkpoint_id: str | None,
    event_seq: int,
    status: DriverStatus,
) -> DriverState:
    return state.model_copy(
        update={
            "invocation_id": invocation_id,
            "checkpoint_id": checkpoint_id,
            "event_seq": event_seq,
            "status": status,
            "updated_at": now_iso(),
        }
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


def _latest_graph_terminal(change_dir: Path, entrypoint: str | None = None) -> tuple[str | None, str | None]:
    """Return ``(invocation_id, terminal)`` for the latest root invocation, if any.

    Scoped to ``entrypoint`` when given, so a completed ``full`` run does not look
    like a completed ``archive``/``retro`` run.
    """
    try:
        store = CheckpointStore(change_dir)
        invocation_id = store.latest_root_invocation(entrypoint)
        if invocation_id is None:
            return None, None
        projection = store.project(invocation_id)
        return invocation_id, projection.terminal
    except Exception:
        return None, None


def evaluate_start_guard(change_dir: Path, entrypoint: str | None = None) -> StartGuard:
    """Refuse duplicate live processes; resume otherwise.

    Graph terminal from the ledger is authoritative. A stale/absent ``driver.json``
    never blocks resume of incomplete work and never forces completed tasks to re-run.

    Completed-invocation refusal is handled in ``GraphRuntime._start_and_drive``
    where the compiled entrypoint ``restart`` policy (``once`` vs ``repeatable``) is
    available.  The driver only refuses a genuinely live PID here.
    """
    existing = read_driver_state(change_dir)
    if existing is not None and existing.status == "running" and is_pid_alive(existing.pid):
        return StartGuard(
            allowed=False,
            reason=f"driver already running (pid {existing.pid}, run_id {existing.run_id})",
            existing=existing,
        )
    if existing is None:
        return StartGuard(allowed=True)
    return StartGuard(allowed=True, existing=existing)
