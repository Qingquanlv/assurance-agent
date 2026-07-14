from pathlib import Path

from click.testing import CliRunner

from assurance_agent.cli import main


def test_init_yes_writes_scaffold(tmp_path: Path) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(main, ["init", "--yes"])
        assert result.exit_code == 0, result.output
        assert "created: .aa/config.yaml" in result.output
        assert Path(".aa/execution-policy.json").is_file()
        assert Path("qa/changes/.gitkeep").is_file()


def test_init_options_override_defaults(tmp_path: Path) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(
            main,
            ["init", "--yes", "--e2e-framework", "none", "--frontend", "./web"],
        )
        assert result.exit_code == 0, result.output
        config = Path(".aa/config.yaml").read_text()
        assert "frontend: ./web" in config
        assert "name: none" in config


def test_init_repair_requires_existing_config(tmp_path: Path) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(main, ["init", "--repair"])
        assert result.exit_code == 1
        assert ".aa/config.yaml not found" in result.output


def test_init_repair_recreates_missing_gitkeep(tmp_path: Path) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        runner.invoke(main, ["init", "--yes"])
        Path("qa/cases/.gitkeep").unlink()
        result = runner.invoke(main, ["init", "--repair"])
        assert result.exit_code == 0, result.output
        assert "created: qa/cases/.gitkeep" in result.output


def test_init_interactive_prompts_for_frameworks(tmp_path: Path) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        # prompts: api framework, e2e framework, enable MCP, confirm
        result = runner.invoke(main, ["init"], input="pytest\nplaywright\nn\ny\n")
        assert result.exit_code == 0, result.output
        assert Path(".aa/config.yaml").is_file()
