"""Temporary GraphRuntime compatibility wrapper for eval callers (until Task 16).

``LoopResult(exit_code, reason)`` is retained. Ownership of workflow progression
lives in ``GraphRuntime``; this module only acquires the process lock, projects
the graph pointer into ``driver.json``, and delegates run/resume.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from assurance_agent.change_location import ChangeNotFoundError, resolve_change
from assurance_agent.config import ConfigNotFoundError
from assurance_agent.identifiers import UnsafeIdentifierError
from assurance_agent.workflow.core.events import append_event_best_effort
from assurance_agent.workflow.core.exit_codes import (
    EXIT_COMPLETED,
    EXIT_ERROR,
    EXIT_HUMAN_REVIEW,
    EXIT_STOPPED,
)
from assurance_agent.workflow.driver.adapter import AgentInvoker, DriverError
from assurance_agent.workflow.driver.driver_state import (
    DriverState,
    DriverStatus,
    acquire_lock,
    create_initial_driver_state,
    evaluate_start_guard,
    now_iso,
    project_graph_pointer,
    read_driver_state,
    release_lock,
    write_driver_state,
)
from assurance_agent.workflow.driver.runtime_factory import (
    build_graph_runtime,
    runtime_context_for,
)
from assurance_agent.workflow.graph.runtime import GraphRuntimeError

__all__ = [
    "EXIT_COMPLETED",
    "EXIT_STOPPED",
    "EXIT_HUMAN_REVIEW",
    "EXIT_ERROR",
    "CliPhaseExecutor",
    "HealingActionExecutor",
    "LoopResult",
    "build_driver_telemetry",
    "run_workflow_loop",
]


class CliPhaseExecutor(Protocol):
    """Typing stub retained for eval imports until Task 16; not injectable."""

    def run_cli_phase(self, entry: object, ctx: object) -> object: ...

    def apply_phase_state(self, entry: object, ctx: object, attempt_id: str) -> object: ...


class HealingActionExecutor(Protocol):
    """Typing stub retained for eval imports until Task 16; not injectable."""

    def execute(self, action: object, ctx: object) -> object: ...



@dataclass
class LoopResult:
    exit_code: int
    reason: str
    driver: DriverState | None = None


def build_driver_telemetry(event_type: str, run_id: str, **extra: object) -> dict:
    event: dict = {"type": event_type, "run_id": run_id, "ts": now_iso(), "source": "driver"}
    event.update({key: value for key, value in extra.items() if value is not None})
    return event


def _driver_status_for(graph_status: str) -> DriverStatus:
    if graph_status == "completed":
        return "completed"
    if graph_status == "interrupted":
        return "paused"
    if graph_status in {"stopped", "failed"}:
        return "failed"
    return "running"


def run_workflow_loop(
    *,
    project_root: Path,
    change_id: str,
    entrypoint: str,
    adapter: AgentInvoker,
    params: dict[str, object] | None = None,
    explicit_schema: Path | None = None,
    parent_session_id: str | None = None,
    skip_lock: bool = False,
    adopt_lock_token: str | None = None,
) -> LoopResult:
    params = params or {}
    try:
        loc = resolve_change(project_root, change_id)
    except (UnsafeIdentifierError, ChangeNotFoundError, ConfigNotFoundError) as err:
        return LoopResult(EXIT_ERROR, str(err))
    change_dir = loc.path

    if adopt_lock_token:
        existing_driver = read_driver_state(change_dir)
        if existing_driver is None or existing_driver.start_token != adopt_lock_token:
            return LoopResult(EXIT_ERROR, "adopt-lock token mismatch or missing driver.json")
        driver = existing_driver
        driver.pid = os.getpid()
        driver.status = "running"
        driver.updated_at = now_iso()
        if parent_session_id:
            driver.parent_session_id = parent_session_id
        owns_lock = True
    else:
        guard = evaluate_start_guard(change_dir)
        if not guard.allowed:
            return LoopResult(EXIT_ERROR, guard.reason or "start refused")
        driver = create_initial_driver_state(
            directory=str(project_root),
            parent_session_id=parent_session_id,
            existing_run_id=guard.existing.run_id if guard.existing else None,
        )
        if guard.existing is not None:
            driver.invocation_id = guard.existing.invocation_id
            driver.checkpoint_id = guard.existing.checkpoint_id
            driver.event_seq = guard.existing.event_seq
        owns_lock = not skip_lock
        if not skip_lock:
            try:
                acquire_lock(change_dir, driver.start_token)
            except DriverError as err:
                return LoopResult(EXIT_ERROR, str(err))

    active: DriverState = driver

    try:
        write_driver_state(change_dir, active)
    except Exception as err:
        if owns_lock:
            release_lock(change_dir)
        return LoopResult(EXIT_ERROR, f"failed to persist initial driver state: {err}", active)
    append_event_best_effort(
        change_dir,
        build_driver_telemetry("driver_started", active.run_id, entrypoint=entrypoint),
    )

    def finish(exit_code: int, reason: str, status: DriverStatus) -> LoopResult:
        nonlocal active
        active.status = status
        active.updated_at = now_iso()
        try:
            write_driver_state(change_dir, active)
            append_event_best_effort(
                change_dir,
                build_driver_telemetry(
                    "driver_finished",
                    active.run_id,
                    exit_code=exit_code,
                    detail=reason,
                ),
            )
        except Exception as err:  # persistence failure must not strand the lock
            exit_code = EXIT_ERROR
            reason = f"{reason}; failed to persist final driver state: {err}"
            active.status = "failed"
        finally:
            if owns_lock:
                release_lock(change_dir)
        return LoopResult(exit_code, reason, active)

    try:
        bundle = build_graph_runtime(
            project_root=project_root,
            change_id=change_id,
            adapter=adapter,
            explicit_schema=explicit_schema,
        )
        context = runtime_context_for(project_root, change_id, params, parent_session_id)
        latest = bundle.runtime.latest_root_invocation()
        result = (
            bundle.runtime.run(bundle.compiled, entrypoint, context)
            if latest is None
            else bundle.runtime.resume(latest)
        )
        active = project_graph_pointer(
            active,
            invocation_id=result.invocation_id,
            checkpoint_id=result.status.checkpoint_id,
            event_seq=result.status.event_seq,
            status=_driver_status_for(result.status.status),
        )
        return finish(result.exit_code, result.reason, active.status)
    except GraphRuntimeError as err:
        return finish(EXIT_ERROR, str(err), "failed")
    except Exception as err:  # noqa: BLE001 — driver must never leak; surface as EXIT_ERROR
        return finish(EXIT_ERROR, str(err), "failed")
