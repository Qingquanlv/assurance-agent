import json
from pathlib import Path

from tests.helpers_aa import write_aa_config

from click.testing import CliRunner

import assurance_agent.commands.workflow_cmd as wf
from assurance_agent.cli import main
from assurance_agent.workflow.driver.driver_state import create_initial_driver_state, write_driver_state
from assurance_agent.workflow.driver.loop import (
    EXIT_COMPLETED,
    EXIT_ERROR,
    EXIT_HUMAN_REVIEW,
    EXIT_STOPPED,
    LoopResult,
)
from assurance_agent.workflow.driver.workflow_start import StartResult


def _patch_loop(monkeypatch, result: LoopResult) -> None:
    monkeypatch.setattr(wf, "run_workflow_loop", lambda **kwargs: result)


def test_workflow_run_maps_completed(monkeypatch) -> None:
    _patch_loop(monkeypatch, LoopResult(EXIT_COMPLETED, "done"))
    with CliRunner().isolated_filesystem():
        result = CliRunner().invoke(main, ["workflow", "run", "--change", "CH-1", "--adapter", "headless"])
        assert result.exit_code == EXIT_COMPLETED


def test_workflow_run_maps_stopped(monkeypatch) -> None:
    _patch_loop(monkeypatch, LoopResult(EXIT_STOPPED, "stopped"))
    with CliRunner().isolated_filesystem():
        result = CliRunner().invoke(main, ["workflow", "run", "--change", "CH-1", "--adapter", "headless"])
        assert result.exit_code == EXIT_STOPPED


def test_workflow_run_maps_human_review(monkeypatch) -> None:
    _patch_loop(monkeypatch, LoopResult(EXIT_HUMAN_REVIEW, "decide"))
    with CliRunner().isolated_filesystem():
        result = CliRunner().invoke(main, ["workflow", "run", "--change", "CH-1", "--adapter", "headless"])
        assert result.exit_code == EXIT_HUMAN_REVIEW


def test_workflow_run_maps_error(monkeypatch) -> None:
    _patch_loop(monkeypatch, LoopResult(EXIT_ERROR, "kaboom"))
    with CliRunner().isolated_filesystem():
        result = CliRunner().invoke(main, ["workflow", "run", "--change", "CH-1", "--adapter", "headless"])
        assert result.exit_code == EXIT_ERROR


def test_workflow_run_bad_params_exit_40() -> None:
    with CliRunner().isolated_filesystem():
        result = CliRunner().invoke(
            main, ["workflow", "run", "--change", "CH-1", "--adapter", "headless", "--params", "{bad"]
        )
        assert result.exit_code == EXIT_ERROR
        assert "Invalid --params JSON" in result.output


def test_workflow_run_opencode_requires_server_exit_40() -> None:
    with CliRunner().isolated_filesystem():
        result = CliRunner().invoke(main, ["workflow", "run", "--change", "CH-1", "--adapter", "opencode"])
        assert result.exit_code == EXIT_ERROR
        assert "--server is required" in result.output


def test_workflow_status_reads_driver_state_json() -> None:
    with CliRunner().isolated_filesystem():
        write_aa_config(Path.cwd())
        change_dir = Path("qa/changes/CH-1")
        change_dir.mkdir(parents=True)
        state = create_initial_driver_state(directory=".")
        state.current_phase = "explore"
        write_driver_state(change_dir, state)
        result = CliRunner().invoke(main, ["workflow", "status", "--change", "CH-1", "--json"])
        assert result.exit_code == 0
        doc = json.loads(result.output)
        assert doc["driver"]["current_phase"] == "explore"
        assert doc["driver"]["run_id"] == state.run_id


def test_workflow_status_no_driver_state_json_null() -> None:
    with CliRunner().isolated_filesystem():
        write_aa_config(Path.cwd())
        (Path("qa/changes/CH-1")).mkdir(parents=True)
        result = CliRunner().invoke(main, ["workflow", "status", "--change", "CH-1", "--json"])
        assert result.exit_code == 0
        assert json.loads(result.output)["driver"] is None


def test_workflow_run_detach_success_exit_0(monkeypatch) -> None:
    monkeypatch.setattr(
        wf, "start_workflow_detached", lambda **kwargs: StartResult(ok=True, message="started", pid=9)
    )
    with CliRunner().isolated_filesystem():
        result = CliRunner().invoke(
            main, ["workflow", "run", "--change", "CH-1", "--adapter", "headless", "--detach"]
        )
        assert result.exit_code == EXIT_COMPLETED
        assert "started" in result.output


def test_workflow_run_detach_failure_exit_40(monkeypatch) -> None:
    monkeypatch.setattr(
        wf, "start_workflow_detached", lambda **kwargs: StartResult(ok=False, message="refused")
    )
    with CliRunner().isolated_filesystem():
        result = CliRunner().invoke(
            main, ["workflow", "run", "--change", "CH-1", "--adapter", "headless", "--detach"]
        )
        assert result.exit_code == EXIT_ERROR
        assert "refused" in result.output


def test_workflow_run_detach_delegates_to_detached_start(monkeypatch) -> None:
    seen: dict = {}

    def fake_start(**kwargs):
        seen.update(kwargs)
        return StartResult(ok=True, message="started", pid=7)

    monkeypatch.setattr(wf, "start_workflow_detached", fake_start)
    # run_workflow_loop must NOT be called on the --detach path.
    monkeypatch.setattr(
        wf, "run_workflow_loop", lambda **k: (_ for _ in ()).throw(AssertionError("loop ran"))
    )
    with CliRunner().isolated_filesystem():
        result = CliRunner().invoke(
            main, ["workflow", "run", "--change", "CH-1", "--adapter", "headless", "--detach"]
        )
        assert result.exit_code == EXIT_COMPLETED
        assert "started" in result.output
        assert seen["change_id"] == "CH-1"


def test_workflow_run_detach_conflicts_with_adopt_lock() -> None:
    with CliRunner().isolated_filesystem():
        result = CliRunner().invoke(
            main,
            ["workflow", "run", "--change", "CH-1", "--detach", "--adopt-lock", "tok"],
        )
        assert result.exit_code != EXIT_COMPLETED
        assert "cannot be combined" in result.output
