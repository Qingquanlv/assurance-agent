"""Legacy CLI workflow smoke tests updated for GraphRuntime cutover."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from tests.helpers_aa import write_aa_config

import assurance_agent.commands.workflow_cmd as wf
from assurance_agent.cli import main
from assurance_agent.workflow.driver.loop import (
    EXIT_COMPLETED,
    EXIT_ERROR,
    EXIT_HUMAN_REVIEW,
    EXIT_STOPPED,
    LoopResult,
)
from assurance_agent.workflow.driver.workflow_start import StartResult
from assurance_agent.workflow.graph.models import GraphStatus


def _patch_loop(monkeypatch, result: LoopResult) -> None:
    monkeypatch.setattr(wf, "run_workflow_loop", lambda **kwargs: result)


def test_workflow_run_maps_completed(monkeypatch) -> None:
    _patch_loop(monkeypatch, LoopResult(EXIT_COMPLETED, "done"))
    with CliRunner().isolated_filesystem():
        result = CliRunner().invoke(
            main, ["workflow", "run", "--change", "CH-1", "--entrypoint", "full", "--adapter", "headless"]
        )
        assert result.exit_code == EXIT_COMPLETED


def test_workflow_run_maps_stopped(monkeypatch) -> None:
    _patch_loop(monkeypatch, LoopResult(EXIT_STOPPED, "stopped"))
    with CliRunner().isolated_filesystem():
        result = CliRunner().invoke(
            main, ["workflow", "run", "--change", "CH-1", "--entrypoint", "full", "--adapter", "headless"]
        )
        assert result.exit_code == EXIT_STOPPED


def test_workflow_run_maps_human_review(monkeypatch) -> None:
    _patch_loop(monkeypatch, LoopResult(EXIT_HUMAN_REVIEW, "decide"))
    with CliRunner().isolated_filesystem():
        result = CliRunner().invoke(
            main, ["workflow", "run", "--change", "CH-1", "--entrypoint", "full", "--adapter", "headless"]
        )
        assert result.exit_code == EXIT_HUMAN_REVIEW


def test_workflow_run_maps_error(monkeypatch) -> None:
    _patch_loop(monkeypatch, LoopResult(EXIT_ERROR, "kaboom"))
    with CliRunner().isolated_filesystem():
        result = CliRunner().invoke(
            main, ["workflow", "run", "--change", "CH-1", "--entrypoint", "full", "--adapter", "headless"]
        )
        assert result.exit_code == EXIT_ERROR


def test_workflow_run_bad_params_exit_40() -> None:
    with CliRunner().isolated_filesystem():
        result = CliRunner().invoke(
            main,
            [
                "workflow",
                "run",
                "--change",
                "CH-1",
                "--adapter",
                "headless",
                "--params",
                "{bad",
            ],
        )
        assert result.exit_code == EXIT_ERROR
        assert "Invalid --params JSON" in result.output


def test_workflow_run_rejects_scope() -> None:
    with CliRunner().isolated_filesystem():
        result = CliRunner().invoke(main, ["workflow", "run", "--change", "CH-1", "--scope", "execute"])
        assert result.exit_code != EXIT_COMPLETED
        assert (
            "no such option" in result.output.lower()
            or "no such option" in str(result.exception).lower()
            or result.exit_code == 2
        )


def test_workflow_run_opencode_requires_server_exit_40() -> None:
    with CliRunner().isolated_filesystem():
        result = CliRunner().invoke(main, ["workflow", "run", "--change", "CH-1", "--adapter", "opencode"])
        assert result.exit_code == EXIT_ERROR
        assert "--server is required" in result.output


def test_workflow_status_alias_warns_and_matches_aa_status() -> None:
    with CliRunner().isolated_filesystem():
        write_aa_config(Path.cwd())
        (Path("qa/changes/CH-1")).mkdir(parents=True)
        alias = CliRunner().invoke(main, ["workflow", "status", "--change", "CH-1", "--json"])
        direct = CliRunner().invoke(main, ["status", "--change", "CH-1", "--json"])
        assert alias.exit_code == direct.exit_code == 0
        assert alias.stdout == direct.stdout  # 逐字节等价（不含 stderr warning）
        assert json.loads(alias.stdout) == {"status": None, "invocation_id": None}
        assert "deprecated" in alias.stderr


def test_workflow_status_alias_supports_next(monkeypatch) -> None:
    from assurance_agent.commands import status_cmd

    monkeypatch.setattr(
        status_cmd,
        "read_latest_graph_status",
        lambda *a, **k: GraphStatus(
            invocation_id="inv-9",
            entrypoint="full",
            status="completed",
            checkpoint_id="cp-9",
            event_seq=4,
            superstep=2,
            running_tasks=(),
            pending_tasks=(),
            pending_write_sets=(),
            pending_interrupts=(),
            next_retry_at=None,
            budgets={},
            terminal_reason="done",
        ),
    )
    with CliRunner().isolated_filesystem():
        write_aa_config(Path.cwd())
        (Path("qa/changes/CH-1")).mkdir(parents=True)
        alias = CliRunner().invoke(main, ["workflow", "status", "--change", "CH-1", "--next", "--json"])
        direct = CliRunner().invoke(main, ["status", "--change", "CH-1", "--next", "--json"])
        assert alias.exit_code == direct.exit_code == 0
        assert alias.stdout == direct.stdout
        assert "deprecated" in alias.stderr


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


def test_workflow_start_is_detached_alias(monkeypatch) -> None:
    seen: dict = {}

    def fake_start(**kwargs):
        seen.update(kwargs)
        return StartResult(ok=True, message="started", pid=7)

    monkeypatch.setattr(wf, "start_workflow_detached", fake_start)
    monkeypatch.setattr(
        wf, "run_workflow_loop", lambda **k: (_ for _ in ()).throw(AssertionError("loop ran"))
    )
    with CliRunner().isolated_filesystem():
        result = CliRunner().invoke(
            main, ["workflow", "start", "--change", "CH-1", "--entrypoint", "full", "--adapter", "headless"]
        )
        assert result.exit_code == EXIT_COMPLETED
        assert seen["entrypoint"] == "full"


def test_workflow_run_detach_conflicts_with_adopt_lock() -> None:
    with CliRunner().isolated_filesystem():
        result = CliRunner().invoke(
            main,
            ["workflow", "run", "--change", "CH-1", "--detach", "--adopt-lock", "tok"],
        )
        assert result.exit_code != EXIT_COMPLETED
        assert "cannot be combined" in result.output
