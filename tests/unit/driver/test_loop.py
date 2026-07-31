"""run_workflow_loop is a GraphRuntime compatibility wrapper."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tests.helpers_aa import write_aa_config

from assurance_agent.workflow.driver import loop as loop_mod
from assurance_agent.workflow.driver.driver_state import read_driver_state
from assurance_agent.workflow.driver.loop import (
    EXIT_COMPLETED,
    EXIT_ERROR,
    EXIT_HUMAN_REVIEW,
    EXIT_STOPPED,
    run_workflow_loop,
)
from assurance_agent.workflow.graph.agent_api import AgentRequest, AgentResult
from assurance_agent.workflow.graph.models import GraphStatus, RunResult


class NeverCalledInvoker:
    def invoke(self, request: AgentRequest) -> AgentResult:
        raise AssertionError(f"unexpected agent invoke: {request}")


def _graph_status(status: str, **overrides: object) -> GraphStatus:
    payload = {
        "invocation_id": "inv-1",
        "entrypoint": "full",
        "status": status,
        "checkpoint_id": "cp-1",
        "event_seq": 1,
        "superstep": 1,
        "running_tasks": (),
        "pending_tasks": (),
        "pending_write_sets": (),
        "pending_interrupts": (),
        "next_retry_at": None,
        "budgets": {},
        "terminal_reason": status,
    }
    payload.update(overrides)
    return GraphStatus(**payload)  # type: ignore[arg-type]


def _run_result(exit_code: int, status: str, reason: str) -> RunResult:
    return RunResult(
        invocation_id="inv-1",
        status=_graph_status(status),
        exit_code=exit_code,  # type: ignore[arg-type]
        reason=reason,
    )


def _patch_bundle(
    monkeypatch: pytest.MonkeyPatch,
    *,
    latest: str | None,
    result: RunResult,
    latest_terminal: str | None = None,
    restart: str = "once",
    drive_error: Exception | None = None,
) -> MagicMock:
    runtime = MagicMock()
    runtime.latest_root_invocation.return_value = latest
    runtime.invocation_terminal.return_value = latest_terminal
    runtime.start_invocation.return_value = result.invocation_id
    if drive_error is not None:
        runtime.drive_started.side_effect = drive_error
    else:
        runtime.drive_started.return_value = result
    runtime.run.return_value = result
    runtime.resume.return_value = result
    entrypoint = MagicMock()
    entrypoint.restart = restart
    compiled = MagicMock()
    compiled.entrypoints.get.return_value = entrypoint
    bundle = MagicMock()
    bundle.runtime = runtime
    bundle.compiled = compiled
    monkeypatch.setattr(loop_mod, "build_graph_runtime", lambda **_kwargs: bundle)
    monkeypatch.setattr(
        loop_mod,
        "runtime_context_for",
        lambda project_root, change_id, params, parent_session_id=None: MagicMock(),
    )
    return runtime


def _prepare(tmp_path: Path) -> None:
    write_aa_config(tmp_path)
    (tmp_path / "qa" / "changes" / "CH-1").mkdir(parents=True, exist_ok=True)


def test_completed_path_exit_0(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _prepare(tmp_path)
    runtime = _patch_bundle(monkeypatch, latest=None, result=_run_result(EXIT_COMPLETED, "completed", "done"))
    result = run_workflow_loop(
        project_root=tmp_path,
        change_id="CH-1",
        entrypoint="full",
        adapter=NeverCalledInvoker(),
    )
    assert result.exit_code == EXIT_COMPLETED
    runtime.start_invocation.assert_called_once()
    runtime.drive_started.assert_called_once_with("inv-1")
    runtime.resume.assert_not_called()
    driver = read_driver_state(tmp_path / "qa" / "changes" / "CH-1")
    assert driver is not None
    assert driver.status == "completed"
    assert driver.invocation_id == "inv-1"
    assert driver.checkpoint_id == "cp-1"
    assert driver.event_seq == 1
    assert not (tmp_path / "qa/changes/CH-1/driver.lock").exists()


def test_resume_when_invocation_exists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _prepare(tmp_path)
    runtime = _patch_bundle(
        monkeypatch,
        latest="inv-existing",
        latest_terminal=None,  # still active → resume
        result=_run_result(EXIT_COMPLETED, "completed", "done"),
    )
    result = run_workflow_loop(
        project_root=tmp_path,
        change_id="CH-1",
        entrypoint="full",
        adapter=NeverCalledInvoker(),
    )
    assert result.exit_code == EXIT_COMPLETED
    runtime.start_invocation.assert_not_called()
    runtime.resume.assert_called_once_with("inv-existing")


def test_once_entrypoint_refuses_completed_restart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _prepare(tmp_path)
    runtime = _patch_bundle(
        monkeypatch,
        latest="inv-done",
        latest_terminal="completed",
        restart="once",
        result=_run_result(EXIT_COMPLETED, "completed", "done"),
    )
    result = run_workflow_loop(
        project_root=tmp_path,
        change_id="CH-1",
        entrypoint="full",
        adapter=NeverCalledInvoker(),
    )
    assert result.exit_code == EXIT_ERROR
    assert result.reason is not None and "already completed" in result.reason
    runtime.start_invocation.assert_not_called()
    runtime.resume.assert_not_called()


def test_stopped_path_exit_20(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _prepare(tmp_path)
    _patch_bundle(monkeypatch, latest=None, result=_run_result(EXIT_STOPPED, "stopped", "halted"))
    result = run_workflow_loop(
        project_root=tmp_path,
        change_id="CH-1",
        entrypoint="full",
        adapter=NeverCalledInvoker(),
    )
    assert result.exit_code == EXIT_STOPPED
    driver = read_driver_state(tmp_path / "qa" / "changes" / "CH-1")
    assert driver is not None and driver.status == "failed"


def test_interrupted_path_exit_30(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _prepare(tmp_path)
    _patch_bundle(monkeypatch, latest=None, result=_run_result(EXIT_HUMAN_REVIEW, "interrupted", "decide"))
    result = run_workflow_loop(
        project_root=tmp_path,
        change_id="CH-1",
        entrypoint="full",
        adapter=NeverCalledInvoker(),
    )
    assert result.exit_code == EXIT_HUMAN_REVIEW
    driver = read_driver_state(tmp_path / "qa" / "changes" / "CH-1")
    assert driver is not None and driver.status == "paused"


def test_runtime_error_exit_40(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _prepare(tmp_path)
    from assurance_agent.workflow.graph.runtime import GraphRuntimeError

    runtime = MagicMock()
    runtime.latest_root_invocation.return_value = None
    runtime.run.side_effect = GraphRuntimeError("boom")
    runtime.start_invocation.side_effect = GraphRuntimeError("boom")
    bundle = MagicMock(runtime=runtime, compiled=MagicMock())
    monkeypatch.setattr(loop_mod, "build_graph_runtime", lambda **_kwargs: bundle)
    monkeypatch.setattr(
        loop_mod,
        "runtime_context_for",
        lambda *args, **kwargs: MagicMock(),
    )
    result = run_workflow_loop(
        project_root=tmp_path,
        change_id="CH-1",
        entrypoint="full",
        adapter=NeverCalledInvoker(),
    )
    assert result.exit_code == EXIT_ERROR
    assert "boom" in result.reason
    assert not (tmp_path / "qa/changes/CH-1/driver.lock").exists()


def test_final_state_persistence_failure_releases_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prepare(tmp_path)
    _patch_bundle(monkeypatch, latest=None, result=_run_result(EXIT_COMPLETED, "completed", "done"))
    real_write = loop_mod.write_driver_state
    calls = 0

    def fail_on_final(change_dir, state):  # noqa: ANN001, ANN202
        nonlocal calls
        calls += 1
        if calls >= 2:
            raise OSError("disk full")
        return real_write(change_dir, state)

    monkeypatch.setattr(loop_mod, "write_driver_state", fail_on_final)
    result = run_workflow_loop(
        project_root=tmp_path,
        change_id="CH-1",
        entrypoint="full",
        adapter=NeverCalledInvoker(),
    )
    assert result.exit_code == EXIT_ERROR
    assert "failed to persist final driver state" in result.reason
    assert not (tmp_path / "qa/changes/CH-1/driver.lock").exists()


def test_initial_state_persistence_failure_releases_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prepare(tmp_path)

    def fail_write(change_dir, state):  # noqa: ANN001, ANN202
        raise OSError("disk full")

    monkeypatch.setattr(loop_mod, "write_driver_state", fail_write)
    result = run_workflow_loop(
        project_root=tmp_path,
        change_id="CH-1",
        entrypoint="full",
        adapter=NeverCalledInvoker(),
    )
    assert result.exit_code == EXIT_ERROR
    assert "failed to persist initial driver state" in result.reason
    assert not (tmp_path / "qa/changes/CH-1/driver.lock").exists()


def test_unsafe_change_id_is_rejected(tmp_path: Path) -> None:
    result = run_workflow_loop(
        project_root=tmp_path,
        change_id="../outside",
        entrypoint="full",
        adapter=NeverCalledInvoker(),
    )
    assert result.exit_code == EXIT_ERROR
    assert "unsafe change id" in result.reason


def test_adopt_lock_token_mismatch(tmp_path: Path) -> None:
    _prepare(tmp_path)
    result = run_workflow_loop(
        project_root=tmp_path,
        change_id="CH-1",
        entrypoint="full",
        adapter=NeverCalledInvoker(),
        adopt_lock_token="missing-token",
    )
    assert result.exit_code == EXIT_ERROR
    assert "adopt-lock" in result.reason


def test_on_root_bound_fires_after_start_before_drive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prepare(tmp_path)
    order: list[str] = []
    runtime_mock = _patch_bundle(
        monkeypatch, latest=None, result=_run_result(EXIT_COMPLETED, "completed", "done")
    )
    drive_result = _run_result(EXIT_COMPLETED, "completed", "done")

    def drive_and_record(*args, **kwargs):  # noqa: ANN001, ANN202
        order.append("drive")
        return drive_result

    runtime_mock.drive_started.side_effect = drive_and_record

    def bind(invocation_id: str, entrypoint: str, started_new_root: bool) -> None:
        order.append(f"bind:{invocation_id}:{entrypoint}:{started_new_root}")

    result = run_workflow_loop(
        project_root=tmp_path,
        change_id="CH-1",
        entrypoint="full",
        adapter=NeverCalledInvoker(),
        on_root_bound=bind,
    )
    assert result.exit_code == EXIT_COMPLETED
    assert order == ["bind:inv-1:full:True", "drive"]


def test_drive_fault_preserves_invocation_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from assurance_agent.workflow.graph.runtime import GraphRuntimeError

    _prepare(tmp_path)
    _patch_bundle(
        monkeypatch,
        latest=None,
        result=_run_result(EXIT_ERROR, "failed", "boom"),
        drive_error=GraphRuntimeError("drive failed"),
    )
    result = run_workflow_loop(
        project_root=tmp_path,
        change_id="CH-1",
        entrypoint="full",
        adapter=NeverCalledInvoker(),
    )
    assert result.exit_code == EXIT_ERROR
    assert result.invocation_id == "inv-1"
    assert result.started_new_root is True
    assert "drive failed" in result.reason


def test_resume_existing_root_does_not_mark_started_new_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prepare(tmp_path)
    _patch_bundle(
        monkeypatch,
        latest="inv-existing",
        latest_terminal=None,
        result=_run_result(EXIT_COMPLETED, "completed", "done"),
    )
    result = run_workflow_loop(
        project_root=tmp_path,
        change_id="CH-1",
        entrypoint="full",
        adapter=NeverCalledInvoker(),
    )
    assert result.started_new_root is False
    assert result.invocation_id == "inv-1"


def test_default_executors_removed() -> None:
    assert not hasattr(loop_mod, "DefaultCliPhaseExecutor")
    assert not hasattr(loop_mod, "DefaultHealingActionExecutor")
    assert not hasattr(loop_mod, "CliPhaseExecutor")
    assert not hasattr(loop_mod, "HealingActionExecutor")
    assert "cli_executor" not in run_workflow_loop.__code__.co_varnames
    assert "healing_executor" not in run_workflow_loop.__code__.co_varnames
