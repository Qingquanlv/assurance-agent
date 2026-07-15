import json
from pathlib import Path

from click.testing import CliRunner

from assurance_agent.cli import main


def test_risk_context_stdout_mode_prints_json_and_does_not_write() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(main, ["risk", "context", "--change", "CH-1", "--stdout"])
        assert result.exit_code == 0, result.output
        doc = json.loads(result.output)
        assert doc["change_id"] == "CH-1"
        assert doc["schema_version"] == "1.0"
        assert not Path("qa/changes/CH-1/explore/context.json").exists()


def test_risk_context_writes_context_json() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path("qa/changes/CH-1").mkdir(parents=True)
        result = runner.invoke(main, ["risk", "context", "--change", "CH-1"])
        assert result.exit_code == 0, result.output
        out = Path("qa/changes/CH-1/explore/context.json")
        assert out.is_file()
        doc = json.loads(out.read_text())
        assert doc["change_id"] == "CH-1"


def test_risk_context_rejects_unsafe_change_id() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(main, ["risk", "context", "--change", "../escape", "--stdout"])
        assert result.exit_code == 1
        assert "change id" in result.output.lower()
