from click.testing import CliRunner

from assurance_agent.cli import main


def test_heal_rejects_non_assurance_product() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["--product", "sample", "heal", "validate-proposal", "--change", "CH-X"])
    assert result.exit_code != 0
    assert "product" in result.output


def test_eval_rejects_non_assurance_product() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["--product", "sample", "eval", "report", "--run", "X"])
    assert result.exit_code != 0
    assert "product" in result.output


def test_improvement_rejects_non_assurance_product() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["--product", "sample", "improvement", "list"])
    assert result.exit_code != 0
    assert "product" in result.output


def test_retro_rejects_non_assurance_product() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["--product", "sample", "retro", "show", "--retro-id", "x"])
    assert result.exit_code != 0
    assert "product" in result.output
