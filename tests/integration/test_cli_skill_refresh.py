from pathlib import Path

from click.testing import CliRunner

from assurance_agent.cli import main


def _init(runner: CliRunner) -> None:
    assert runner.invoke(main, ["init", "--yes"]).exit_code == 0


def test_refresh_without_project_exits_1(tmp_path: Path) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(main, ["skill", "refresh"])
        assert result.exit_code == 1
        assert "No assurance-agent project" in result.output


def test_refresh_creates_skills_then_idempotent(tmp_path: Path) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        _init(runner)
        first = runner.invoke(main, ["skill", "refresh"])
        assert first.exit_code == 0, first.output
        assert Path("skills/aa-workflow/SKILL.md").is_file()
        assert len(list(Path("skills").iterdir())) == 38

        second = runner.invoke(main, ["skill", "refresh"])
        assert second.exit_code == 0
        assert "0 created, 0 updated" in second.output


def test_refresh_sync_agents_lays_down_opencode(tmp_path: Path) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        _init(runner)
        result = runner.invoke(main, ["skill", "refresh", "--sync-agents"])
        assert result.exit_code == 0, result.output
        assert Path(".opencode/agents/aa-doc-author.md").is_file()
        assert Path(".opencode/tools/workflow_start.ts").is_file()
        assert Path(".opencode/plugins/aa.mjs").is_file()


def test_refresh_dry_run_writes_nothing(tmp_path: Path) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        Path(".aa").mkdir()
        Path(".aa/config.yaml").write_text("version: 1\n", encoding="utf-8")
        result = runner.invoke(main, ["skill", "refresh", "--dry-run"])
        assert result.exit_code == 0
        assert not Path("skills").exists()
