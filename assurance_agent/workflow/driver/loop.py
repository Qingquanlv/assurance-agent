"""Deterministic workflow dispatch loop (spec 5a).

Each iteration asks compute_status for typed healing actions, dispatches and a
terminal. Strict audit writes use only M3's frozen union; driver lifecycle is
best-effort telemetry. Ordinary DAG completion remains produces/gate-derived.
"""

import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from assurance_agent.change_location import ChangeNotFoundError, resolve_change
from assurance_agent.config import ConfigNotFoundError
from assurance_agent.exceptions import AaError
from assurance_agent.identifiers import UnsafeIdentifierError
from assurance_agent.workflow.core.events import append_event_best_effort
from assurance_agent.workflow.core.exit_codes import (
    EXIT_COMPLETED,
    EXIT_ERROR,
    EXIT_HUMAN_REVIEW,
    EXIT_STOPPED,
)
from assurance_agent.workflow.core.progression import ProgressionError, ProgressionRollbackError
from assurance_agent.workflow.core.state import read_state
from assurance_agent.workflow.driver.adapter import Adapter, DriverError, PhaseRequest, PhaseResult
from assurance_agent.workflow.driver.driver_state import (
    DriverState,
    acquire_lock,
    create_initial_driver_state,
    evaluate_start_guard,
    now_iso,
    read_driver_state,
    release_lock,
    write_driver_state,
)
from assurance_agent.workflow.driver.phase_prompt import build_phase_prompt
from assurance_agent.workflow.driver.process_runner import (
    ProcessRunner,
    SubprocessRunner,
    resolve_aa_command,
)
from assurance_agent.workflow.orchestration.engine import (
    DispatchEntry,
    Terminal,
    WorkflowStatus,
    compute_status,
)
from assurance_agent.workflow.orchestration.healing_episode import HealingEpisodeAction
from assurance_agent.workflow.orchestration.operations import (
    allocate_healing_attempt,
    apply_phase_outcome,
    record_dispatch,
)
from assurance_agent.workflow.orchestration.schema import WorkflowSchema, load_workflow_schema

__all__ = [
    "EXIT_COMPLETED",
    "EXIT_STOPPED",
    "EXIT_HUMAN_REVIEW",
    "EXIT_ERROR",
    "PhaseContext",
    "CliPhaseExecutor",
    "DefaultCliPhaseExecutor",
    "HealingActionExecutor",
    "DefaultHealingActionExecutor",
    "LoopResult",
    "build_driver_telemetry",
    "run_workflow_loop",
]

StatusProvider = Callable[[], WorkflowStatus]


@dataclass
class PhaseContext:
    change_id: str
    change_dir: Path
    project_root: Path
    params: dict
    parent_session_id: str | None = None


@dataclass
class LoopResult:
    exit_code: int
    reason: str
    driver: DriverState | None = None


def build_driver_telemetry(event_type: str, run_id: str, **extra: object) -> dict:
    event: dict = {"type": event_type, "run_id": run_id, "ts": now_iso(), "source": "driver"}
    event.update({key: value for key, value in extra.items() if value is not None})
    return event


class CliPhaseExecutor(Protocol):
    def run_cli_phase(self, entry: DispatchEntry, ctx: PhaseContext) -> PhaseResult: ...

    def apply_phase_state(
        self,
        entry: DispatchEntry,
        ctx: PhaseContext,
        attempt_id: str,
    ) -> PhaseResult:
        """Commit the signed phase outcome and typed presentation state.

        Agents cannot write workflow-state or strict events. Ordinary progress
        remains produces-driven; this boundary guarantees audit completeness and
        supplies the registry gate's typed state evidence.
        """
        ...


class DefaultCliPhaseExecutor:
    """Run cli-kind phases via pinned `aa` CLI; commit outcomes in-process.

    Phase->command mapping is driver-local because DispatchEntry does not carry
    a per-phase executor command. `aa run` exiting non-zero while its execution
    manifest + quality-gate result exist is a gate FAIL (tests ran, some failed)
    and is routed onward, not treated as a driver-fatal error (TS parity).

    State advancement (`apply_phase_state`) calls ``apply_phase_outcome`` in
    process — CLI remains available for manual/agent use, but the driver no
    longer shells out for the write.
    """

    def __init__(
        self,
        runner: ProcessRunner | None = None,
        aa_command: list[str] | None = None,
        timeout: float | None = None,
        schema: WorkflowSchema | None = None,
    ) -> None:
        self._runner = runner or SubprocessRunner()
        self._aa = aa_command or resolve_aa_command()
        self._timeout = timeout
        self._schema = schema

    def apply_phase_state(
        self,
        entry: DispatchEntry,
        ctx: PhaseContext,
        attempt_id: str,
    ) -> PhaseResult:
        schema = self._schema or load_workflow_schema(ctx.project_root)
        try:
            apply_phase_outcome(
                ctx.project_root,
                ctx.change_dir,
                schema,
                entry.phase_id,
                attempt_id=attempt_id,
                skill=entry.skill,
            )
        except ProgressionRollbackError as err:
            return PhaseResult(
                ok=False,
                error=f"phase {entry.phase_id} state apply partial-commit: {err}",
            )
        except (ProgressionError, AaError) as err:
            return PhaseResult(
                ok=False,
                error=f"phase {entry.phase_id} state apply failed: {err}",
            )
        return PhaseResult(ok=True, output=f"applied {entry.phase_id}")

    def run_cli_phase(self, entry: DispatchEntry, ctx: PhaseContext) -> PhaseResult:
        args = self._phase_args(entry.phase_id, ctx.change_id)
        if args is None:
            return PhaseResult(ok=False, output="", error=f"no CLI mapping for cli phase '{entry.phase_id}'")

        run = self._runner.run([*self._aa, *args], ctx.project_root, timeout=self._timeout)
        is_run = args[0] == "run"
        ok = run.exit_code == 0 or (is_run and self._execution_results_present(ctx.change_dir))
        if not ok:
            detail = (run.stderr or run.stdout)[:500]
            return PhaseResult(
                ok=False,
                output=run.stdout,
                error=f"aa {' '.join(args)} failed (exit {run.exit_code}): {detail}",
            )
        # Outcome commit happens in the loop via apply_phase_state, not here.
        return PhaseResult(ok=True, output=run.stdout)

    @staticmethod
    def _phase_args(phase_id: str, change_id: str) -> list[str] | None:
        name = phase_id.lower()
        # execution / healing-rerun both (re)run the test suite → `aa run`.
        if name == "execution" or name.endswith("execution") or name.endswith("-rerun"):
            return ["run", "--change", change_id]
        if "inspect" in name:
            return ["report", "inspect", "--change", change_id]
        if "report" in name:
            return ["report", "generate", "--change", change_id]
        return None

    @staticmethod
    def _execution_results_present(change_dir: Path) -> bool:
        base = change_dir / "execution"
        return (base / "execution-manifest.yaml").is_file() and (base / "quality-gate-result.json").is_file()


class HealingActionExecutor(Protocol):
    def execute(self, action: HealingEpisodeAction, ctx: PhaseContext) -> PhaseResult: ...


class DefaultHealingActionExecutor:
    """Commit M3 healing control actions without recomputing their semantics."""

    def __init__(
        self,
        runner: ProcessRunner | None = None,
        aa_command: list[str] | None = None,
    ) -> None:
        self._runner = runner or SubprocessRunner()
        self._aa = aa_command or resolve_aa_command()

    def execute(self, action: HealingEpisodeAction, ctx: PhaseContext) -> PhaseResult:
        if action.kind == "allocate_attempt" and action.allocation is not None:
            return self._allocate(action, ctx)
        if action.kind == "complete" and action.outcome:
            result = self._runner.run(
                [*self._aa, "state", "heal", "--change", ctx.change_id, "--status", action.outcome],
                ctx.project_root,
            )
            return PhaseResult(
                ok=result.exit_code == 0,
                output=result.stdout,
                error=None if result.exit_code == 0 else (result.stderr or result.stdout)[:500],
            )
        return PhaseResult(ok=False, error=f"unsupported healing action: {action.kind}")

    @staticmethod
    def _allocate(action: HealingEpisodeAction, ctx: PhaseContext) -> PhaseResult:
        allocation = action.allocation
        assert allocation is not None
        try:
            result = allocate_healing_attempt(ctx.change_dir, allocation)
        except ProgressionRollbackError as err:
            return PhaseResult(ok=False, error=f"healing allocation partial-commit: {err}")
        except (ProgressionError, AaError) as err:
            return PhaseResult(ok=False, error=f"healing allocation failed: {err}")
        return PhaseResult(ok=True, output=result.attempt_id)


class _DefaultStatusProvider:
    def __init__(self, schema: WorkflowSchema, change_dir: Path, params: dict, scope: str) -> None:
        self._schema = schema
        self._change_dir = change_dir
        self._params = params
        self._scope = scope

    def __call__(self) -> WorkflowStatus:
        state = read_state(self._change_dir)
        return compute_status(
            self._schema,
            self._change_dir,
            state,
            self._params,
            scope=self._scope,
        )


def _exit_for_terminal(terminal: Terminal) -> int:
    return {
        "completed": EXIT_COMPLETED,
        "stopped": EXIT_STOPPED,
        "needs_human_review": EXIT_HUMAN_REVIEW,
    }.get(terminal.kind, EXIT_ERROR)


def _dispatch_entry(
    entry: DispatchEntry,
    ctx: PhaseContext,
    adapter: Adapter,
    cli_executor: CliPhaseExecutor,
) -> PhaseResult:
    if entry.kind == "orchestrator":
        return PhaseResult(ok=True, output="")
    if entry.kind == "cli":
        return cli_executor.run_cli_phase(entry, ctx)
    if not entry.skill:
        return PhaseResult(ok=False, output="", error=f"skill phase {entry.phase_id} missing skill")
    request = PhaseRequest(
        change_id=ctx.change_id,
        phase_id=entry.phase_id,
        skill=entry.skill,
        agent=entry.agent,
        prompt=build_phase_prompt(entry.skill, entry.phase_id, ctx.change_id),
    )
    return adapter.run_phase(request)


def run_workflow_loop(
    *,
    project_root: Path,
    change_id: str,
    scope: str,
    adapter: Adapter,
    params: dict | None = None,
    parent_session_id: str | None = None,
    schema: WorkflowSchema | None = None,
    status_provider: StatusProvider | None = None,
    cli_executor: CliPhaseExecutor | None = None,
    healing_executor: HealingActionExecutor | None = None,
    max_iterations: int = 50,
    max_phase_attempts: int = 1,
    break_at: str | None = None,
    skip_lock: bool = False,
    adopt_lock_token: str | None = None,
) -> LoopResult:
    params = params or {}
    try:
        change_dir = resolve_change(project_root, change_id).path
    except (UnsafeIdentifierError, ChangeNotFoundError, ConfigNotFoundError) as err:
        return LoopResult(EXIT_ERROR, str(err))
    if status_provider is None:
        schema = schema or load_workflow_schema(project_root)
        status_provider = _DefaultStatusProvider(schema, change_dir, params, scope)
    else:
        schema = schema or load_workflow_schema(project_root)
    cli_executor = cli_executor or DefaultCliPhaseExecutor(schema=schema)
    healing_executor = healing_executor or DefaultHealingActionExecutor()

    # ---- lock / driver-state setup ------------------------------------------
    if adopt_lock_token:
        driver = read_driver_state(change_dir)
        if driver is None or driver.start_token != adopt_lock_token:
            return LoopResult(EXIT_ERROR, "adopt-lock token mismatch or missing driver.json")
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
        owns_lock = not skip_lock
        if not skip_lock:
            try:
                acquire_lock(change_dir, driver.start_token)
            except DriverError as err:
                return LoopResult(EXIT_ERROR, str(err))

    try:
        write_driver_state(change_dir, driver)
    except Exception as err:
        if owns_lock:
            release_lock(change_dir)
        return LoopResult(EXIT_ERROR, f"failed to persist initial driver state: {err}", driver)
    append_event_best_effort(change_dir, build_driver_telemetry("driver_started", driver.run_id, scope=scope))

    def finish(exit_code: int, reason: str, status: str) -> LoopResult:
        driver.status = status  # type: ignore[assignment]
        driver.current_attempt_id = None
        driver.updated_at = now_iso()
        try:
            write_driver_state(change_dir, driver)
            append_event_best_effort(
                change_dir,
                build_driver_telemetry(
                    "driver_finished",
                    driver.run_id,
                    exit_code=exit_code,
                    detail=reason,
                ),
            )
        except Exception as err:  # persistence failure must not strand the lock
            exit_code = EXIT_ERROR
            reason = f"{reason}; failed to persist final driver state: {err}"
            driver.status = "failed"
        finally:
            if owns_lock:
                release_lock(change_dir)
        return LoopResult(exit_code, reason, driver)

    def pause(phase: str, reason: str) -> LoopResult:
        driver.status = "paused"
        driver.paused_on = phase
        driver.updated_at = now_iso()
        exit_code = EXIT_HUMAN_REVIEW
        try:
            write_driver_state(change_dir, driver)
            append_event_best_effort(
                change_dir,
                build_driver_telemetry("driver_paused", driver.run_id, phase=phase, detail=reason),
            )
        except Exception as err:  # persistence failure must not strand the lock
            exit_code = EXIT_ERROR
            reason = f"{reason}; failed to persist paused driver state: {err}"
            driver.status = "failed"
        finally:
            if owns_lock:
                release_lock(change_dir)
        return LoopResult(exit_code, reason, driver)

    ctx = PhaseContext(
        change_id=change_id,
        change_dir=change_dir,
        project_root=project_root,
        params=params,
        parent_session_id=parent_session_id,
    )

    try:
        for _ in range(max_iterations):
            status = status_provider()
            # A human/gate stop always preempts pending control work. A completed
            # status may still carry the final healing `complete(resolved)` action,
            # so that one is committed before returning 0.
            if status.terminal is not None and status.terminal.kind != "completed":
                terminal = status.terminal
                return finish(_exit_for_terminal(terminal), terminal.reason or terminal.kind, "failed")
            handled_control = False
            for action in status.healing_episode.next_actions:
                if action.kind == "dispatch_phase":
                    continue  # mirrored in next_dispatch by M3
                if action.kind == "await_human":
                    return pause("healing", "healing safety needs human review")
                result = healing_executor.execute(action, ctx)
                if not result.ok:
                    return finish(EXIT_ERROR, result.error or f"healing {action.kind} failed", "failed")
                handled_control = True
            if handled_control:
                continue  # allocation/complete changes the ledger; re-project before dispatch
            if status.terminal is not None:
                terminal = status.terminal
                exit_code = _exit_for_terminal(terminal)
                status_name = "completed" if terminal.kind == "completed" else "failed"
                return finish(exit_code, terminal.reason or terminal.kind, status_name)
            if not status.next_dispatch:
                return finish(EXIT_ERROR, "no ready phases but workflow not terminal", "failed")

            for entry in status.next_dispatch:
                if break_at is not None and entry.phase_id == break_at:
                    return pause(entry.phase_id, f"breakpoint before {entry.phase_id}")

                attempt_limit = max(1, max_phase_attempts) if entry.kind == "skill" else 1
                result = PhaseResult(ok=False, output="", error="not attempted")
                successful_attempt_id: str | None = None
                for _attempt_number in range(attempt_limit):
                    # Every physical adapter invocation gets its own signed id.
                    # Only the successful invocation's id crosses the outcome boundary.
                    driver.current_phase = entry.phase_id
                    attempt_id = f"{entry.phase_id}:{uuid4()}"
                    driver.current_attempt_id = attempt_id
                    driver.updated_at = now_iso()
                    write_driver_state(change_dir, driver)
                    try:
                        record_dispatch(
                            change_dir,
                            phase_id=entry.phase_id,
                            kind="dispatch_phase",
                            attempt_id=driver.current_attempt_id,
                            dispatched_at=int(time.time() * 1000),
                        )
                    except (ProgressionError, AaError) as err:
                        return finish(EXIT_ERROR, f"dispatch_signed failed: {err}", "failed")
                    result = _dispatch_entry(entry, ctx, adapter, cli_executor)
                    if result.ok:
                        successful_attempt_id = attempt_id
                        break
                if not result.ok:
                    return finish(EXIT_ERROR, f"phase {entry.phase_id} failed: {result.error}", "failed")
                if successful_attempt_id is None:  # defensive type/runtime invariant
                    return finish(EXIT_ERROR, "successful dispatch missing attempt id", "failed")

                # Commit one frozen outcome for the exact signed attempt. This
                # is mandatory audit/state evidence even though ordinary DAG
                # completion is projected from produces + gate.
                applied = cli_executor.apply_phase_state(
                    entry,
                    ctx,
                    successful_attempt_id,
                )
                if not applied.ok:
                    return finish(
                        EXIT_ERROR,
                        f"phase {entry.phase_id} state apply failed: {applied.error}",
                        "failed",
                    )

                # M4 state apply has committed the frozen phase_outcome_committed
                # for this exact attempt id. No second driver-specific strict event.
                driver.current_attempt_id = None
                driver.updated_at = now_iso()
                write_driver_state(change_dir, driver)

        return finish(EXIT_ERROR, f"max iterations ({max_iterations}) exceeded", "failed")
    except Exception as err:  # noqa: BLE001 — driver must never leak; surface as EXIT_ERROR
        return finish(EXIT_ERROR, str(err), "failed")
