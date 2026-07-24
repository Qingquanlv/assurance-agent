from click.testing import CliRunner

from assurance_agent.cli import main
from assurance_agent.retro.nightly.types import NightlyOptions


def test_nightly_options_has_no_history_field() -> None:
    assert "history" not in NightlyOptions.model_fields


def test_retro_collect_help_has_no_history_flag() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["retro", "nightly", "collect", "--help"])
    assert result.exit_code == 0
    assert "--history" not in result.output
