"""`aa workflow compile` — load and compile without driving a change."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from assurance_agent.cli import main
from assurance_agent.workflow.driver.loop import EXIT_ERROR


_MINIMAL = """\
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


def test_workflow_compile_packaged_prints_digest_and_entrypoints() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(main, ["workflow", "compile", "--json"])
        assert result.exit_code == 0, result.output
        assert '"ok": true' in result.output
        assert '"origin": "packaged"' in result.output
        assert '"full"' in result.output
        assert '"execute"' in result.output


def test_workflow_compile_project_schema_lists_custom_entrypoint() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path(".aa").mkdir()
        Path(".aa/workflow-schema.yaml").write_text(_MINIMAL, encoding="utf-8")
        result = runner.invoke(main, ["workflow", "compile", "--json"])
        assert result.exit_code == 0, result.output
        assert '"origin": "project"' in result.output
        assert '"my-pipeline"' in result.output
        assert '"full"' not in result.output


def test_workflow_compile_explicit_schema_missing_exits_error() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path(".aa").mkdir()
        result = runner.invoke(main, ["workflow", "compile", "--schema", ".aa/missing.yaml"])
        assert result.exit_code == EXIT_ERROR
        assert "not found" in result.output


def test_workflow_compile_rejects_schema_outside_overlay_dirs() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path("custom").mkdir()
        Path("custom/workflow.yaml").write_text(_MINIMAL, encoding="utf-8")
        result = runner.invoke(main, ["workflow", "compile", "--schema", "custom/workflow.yaml"])
        assert result.exit_code == EXIT_ERROR
        assert "must be under .aa/ or schemas/" in result.output


def test_workflow_compile_reports_invalid_schema() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path(".aa").mkdir()
        Path(".aa/workflow-schema.yaml").write_text("name: broken\n", encoding="utf-8")
        result = runner.invoke(main, ["workflow", "compile"])
        assert result.exit_code == EXIT_ERROR
        assert "invalid workflow schema" in result.output.lower() or "error" in result.output.lower()


def test_workflow_compile_explicit_contracts_missing_exits_error() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path(".aa").mkdir()
        Path(".aa/workflow-schema.yaml").write_text(_MINIMAL, encoding="utf-8")
        result = runner.invoke(main, ["workflow", "compile", "--contracts", ".aa/missing-contracts.yaml"])
        assert result.exit_code == EXIT_ERROR
        assert "not found" in result.output
