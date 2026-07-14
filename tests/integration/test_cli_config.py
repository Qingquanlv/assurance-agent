from click.testing import CliRunner

from assurance_agent.cli import main


def test_config_print_outputs_raw_yaml() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        runner.invoke(main, ["init", "--yes"])
        result = runner.invoke(main, ["config", "print"])
        assert result.exit_code == 0
        assert result.output.startswith("version: 1")


def test_config_print_missing_config_exits_1() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(main, ["config", "print"])
        assert result.exit_code == 1
        assert ".aa/config.yaml not found" in result.output
