"""aa gate check — frozen gate report via --node-path (no live --phase re-adjudication)."""

from pathlib import Path

from click.testing import CliRunner

from assurance_agent.cli import main
from tests.helpers_aa import write_aa_config


def test_gate_missing_change_exits_1() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        write_aa_config(Path.cwd())
        result = runner.invoke(main, ["gate", "check", "--change", "NOPE", "--node-path", "x"])
        assert result.exit_code == 1


def test_gate_requires_node_path() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        write_aa_config(Path.cwd())
        (Path("qa/changes/CH-1")).mkdir(parents=True)
        result = runner.invoke(main, ["gate", "check", "--change", "CH-1"])
        assert result.exit_code == 2
        assert "node-path" in result.output.lower() or "required" in result.output.lower()


def test_gate_no_invocation_exits_error() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        write_aa_config(Path.cwd())
        (Path("qa/changes/CH-1")).mkdir(parents=True)
        result = runner.invoke(
            main, ["gate", "check", "--change", "CH-1", "--node-path", "missing", "--json"]
        )
        assert result.exit_code != 0
        assert "invocation" in result.output.lower() or "failed" in result.output.lower()
