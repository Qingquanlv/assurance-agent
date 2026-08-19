"""Legacy CLI workflow smoke tests updated for GraphRuntime cutover."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

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

_MINIMAL_SCHEMA = """\
name: project-custom
entrypoints:
  my-pipeline: {graph: main, restart: repeatable}
policies:
  retry:
    never: {max_attempts: 1, retry_on: []}
  timeout:
    local: {run_seconds: 60, heartbeat_seconds: 10}
  scheduler: {max_parallel_tasks: 1}
graphs:
  main:
    max_supersteps: 4
    nodes:
      noop:
        uses: operation:no-op
        retry: never
        timeout: local
    edges:
      - {from: START, to: noop}
      - {from: noop, to: END}
gates: {}
"""


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


def test_workflow_run_accepts_custom_entrypoint_from_project_schema(monkeypatch) -> None:
    captured: dict = {}

    def fake_loop(**kwargs):
        captured.update(kwargs)
        return LoopResult(EXIT_COMPLETED, "done")

    monkeypatch.setattr(wf, "run_workflow_loop", fake_loop)
    with CliRunner().isolated_filesystem():
        Path(".aa").mkdir()
        Path(".aa/workflow-schema.yaml").write_text(_MINIMAL_SCHEMA, encoding="utf-8")
        result = CliRunner().invoke(
            main,
            [
                "workflow",
                "run",
                "--change",
                "CH-1",
                "--entrypoint",
                "my-pipeline",
                "--adapter",
                "headless",
            ],
        )
        assert result.exit_code == EXIT_COMPLETED, result.output
        assert captured["entrypoint"] == "my-pipeline"


def test_workflow_run_rejects_unknown_entrypoint_listing_loaded_names() -> None:
    with CliRunner().isolated_filesystem():
        Path(".aa").mkdir()
        Path(".aa/workflow-schema.yaml").write_text(_MINIMAL_SCHEMA, encoding="utf-8")
        result = CliRunner().invoke(
            main,
            [
                "workflow",
                "run",
                "--change",
                "CH-1",
                "--entrypoint",
                "full",
                "--adapter",
                "headless",
            ],
        )
        assert result.exit_code == EXIT_ERROR
        assert "unknown entrypoint 'full'" in result.output
        assert "my-pipeline" in result.output


def test_workflow_run_forwards_explicit_schema_and_contracts(monkeypatch) -> None:
    captured: dict = {}

    def fake_loop(**kwargs):
        captured.update(kwargs)
        return LoopResult(EXIT_COMPLETED, "done")

    monkeypatch.setattr(wf, "run_workflow_loop", fake_loop)
    with CliRunner().isolated_filesystem():
        Path(".aa").mkdir()
        Path(".aa/workflow-schema.yaml").write_text(_MINIMAL_SCHEMA, encoding="utf-8")
        Path(".aa/execution-contracts.yaml").write_text(
            "schema_version: '1'\ncontracts: {}\n", encoding="utf-8"
        )
        result = CliRunner().invoke(
            main,
            [
                "workflow",
                "run",
                "--change",
                "CH-1",
                "--entrypoint",
                "my-pipeline",
                "--adapter",
                "headless",
                "--schema",
                ".aa/workflow-schema.yaml",
                "--contracts",
                ".aa/execution-contracts.yaml",
            ],
        )
        assert result.exit_code == EXIT_COMPLETED, result.output
        assert captured["explicit_schema"] is not None
        assert captured["explicit_schema"].name == "workflow-schema.yaml"
        assert captured["explicit_contracts"] is not None
        assert captured["explicit_contracts"].name == "execution-contracts.yaml"


def test_workflow_run_missing_schema_fails_closed() -> None:
    with CliRunner().isolated_filesystem():
        Path(".aa").mkdir()
        result = CliRunner().invoke(
            main,
            [
                "workflow",
                "run",
                "--change",
                "CH-1",
                "--adapter",
                "headless",
                "--schema",
                ".aa/missing.yaml",
            ],
        )
        assert result.exit_code == EXIT_ERROR
        assert "not found" in result.output
