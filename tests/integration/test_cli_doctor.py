import json

from click.testing import CliRunner

from assurance_agent.cli import main


def test_doctor_without_init_exits_1() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(main, ["doctor"])
        assert result.exit_code == 1
        assert ".aa/config.yaml not found" in result.output


def test_doctor_after_init_exits_0_and_reports_groups() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        runner.invoke(main, ["init", "--yes"])
        result = runner.invoke(main, ["doctor"])
        assert result.exit_code == 0, result.output
        assert "Config" in result.output
        assert "Result:" in result.output


def test_doctor_json_is_machine_readable() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        runner.invoke(main, ["init", "--yes"])
        result = runner.invoke(main, ["doctor", "--json"])
        assert result.exit_code == 0
        doc = json.loads(result.output)
        assert doc["status"] in ("ok", "warning")
        assert any(c["id"] == "config.schema" for c in doc["checks"])
