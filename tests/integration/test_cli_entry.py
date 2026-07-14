from click.testing import CliRunner

from assurance_agent.cli import main


def test_version_flag_prints_version() -> None:
    result = CliRunner().invoke(main, ["--version"])
    assert result.exit_code == 0
    assert "aa, version 0.1.0" in result.output


def test_help_lists_program_description() -> None:
    result = CliRunner().invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "Assurance Agent" in result.output
