"""`aa workflow compile` — load and compile without driving a change."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from assurance_agent.cli import main
from assurance_agent.workflow.driver.loop import EXIT_ERROR
from tests.helpers_aa import overlay_workflow_yaml


_MINIMAL = overlay_workflow_yaml(node="noop", uses="operation:no-op")
_SAMPLE_OVERLAY = overlay_workflow_yaml(node="ping", uses="operation:sample-ping")


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


def test_workflow_compile_unknown_product_exits_error() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(main, ["--product", "not-installed", "workflow", "compile"])
        assert result.exit_code != 0
        assert "product" in result.output
        assert "not-installed" in result.output
        assert "No such option" not in result.output
        assert "graph_definition_changed" not in result.output


def test_workflow_compile_default_product_still_packaged_full() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(main, ["workflow", "compile", "--json"])
        assert result.exit_code == 0, result.output
        assert '"origin": "packaged"' in result.output
        assert '"full"' in result.output


def test_workflow_compile_sample_product_lists_ping_not_full() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(main, ["--product", "sample", "workflow", "compile", "--json"])
        assert result.exit_code == 0, result.output
        assert '"ping"' in result.output
        assert '"full"' not in result.output


def test_sample_product_plus_project_overlay_compiles_project_graph() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path(".aa").mkdir()
        Path(".aa/workflow-schema.yaml").write_text(_SAMPLE_OVERLAY, encoding="utf-8")
        result = runner.invoke(main, ["--product", "sample", "workflow", "compile", "--json"])
        assert result.exit_code == 0, result.output
        assert '"origin": "project"' in result.output
        assert '"my-pipeline"' in result.output
