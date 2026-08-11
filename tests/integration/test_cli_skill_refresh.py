import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from assurance_agent import resources
from assurance_agent.commands import skill_cmd
from assurance_agent.cli import main
from assurance_agent.workflow.core import assets
from assurance_agent.workflow.core.assets import PLUGIN_ENTRY


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
        assert len(list(Path("skills").iterdir())) == 39

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
        assert (
            Path(".opencode/skills/aa-case-design/SKILL.md").read_bytes()
            == Path("skills/aa-case-design/SKILL.md").read_bytes()
        )


def test_refresh_sync_agents_restores_missing_plugin_registration(tmp_path: Path) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        _init(runner)
        Path("opencode.json").write_text(json.dumps({"plugin": []}) + "\n", encoding="utf-8")

        result = runner.invoke(main, ["skill", "refresh", "--sync-agents"])

        assert result.exit_code == 0, result.output
        assert json.loads(Path("opencode.json").read_text(encoding="utf-8"))["plugin"] == [PLUGIN_ENTRY]


def test_refresh_can_sync_aa_skills_to_opencode_user_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    user_config = tmp_path / "opencode-user-config"
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(user_config))

    with runner.isolated_filesystem(temp_dir=tmp_path):
        _init(runner)
        result = runner.invoke(main, ["skill", "refresh", "--sync-opencode-user-skills"])

        assert result.exit_code == 0, result.output
        assert (user_config / "skills/aa-case-design/SKILL.md").read_bytes() == Path(
            "skills/aa-case-design/SKILL.md"
        ).read_bytes()
        assert not (user_config / "skills/writing-skills/SKILL.md").exists()


def test_refresh_can_sync_aa_agents_to_opencode_user_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    user_config = tmp_path / "opencode-user-config"
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(user_config))

    with runner.isolated_filesystem(temp_dir=tmp_path):
        _init(runner)
        result = runner.invoke(main, ["skill", "refresh", "--sync-opencode-user-agents"])

        assert result.exit_code == 0, result.output
        assert (user_config / "agents/aa-doc-author.md").read_bytes() == resources.read_text(
            "opencode", "agents", "aa-doc-author.md"
        ).encode("utf-8")


def test_refresh_fails_closed_when_runtime_agent_hash_is_stale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    user_config = tmp_path / "opencode-user-config"
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(user_config))
    sync_runtime = skill_cmd.sync_opencode_user_agents

    def corrupt_runtime_after_sync(
        agents_root: Path | None = None,
        *,
        dry_run: bool = False,
    ) -> assets.SyncResult:
        result = sync_runtime(agents_root, dry_run=dry_run)
        (user_config / "agents/aa-doc-author.md").write_text("stale", encoding="utf-8")
        return result

    monkeypatch.setattr(skill_cmd, "sync_opencode_user_agents", corrupt_runtime_after_sync)
    with runner.isolated_filesystem(temp_dir=tmp_path):
        _init(runner)
        result = runner.invoke(main, ["skill", "refresh", "--sync-opencode-user-agents"])

        assert result.exit_code == 1
        assert "agent sync integrity check failed" in result.output
        assert "aa-doc-author.md" in result.output


def test_refresh_fails_closed_when_runtime_skill_hash_is_stale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    user_config = tmp_path / "opencode-user-config"
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(user_config))
    sync_runtime = skill_cmd.sync_opencode_user_skills

    def corrupt_runtime_after_sync(
        skills_root: Path | None = None,
        *,
        dry_run: bool = False,
    ) -> assets.SyncResult:
        result = sync_runtime(skills_root, dry_run=dry_run)
        (user_config / "skills/aa-case-design/SKILL.md").write_text("stale", encoding="utf-8")
        return result

    monkeypatch.setattr(skill_cmd, "sync_opencode_user_skills", corrupt_runtime_after_sync)
    with runner.isolated_filesystem(temp_dir=tmp_path):
        _init(runner)
        result = runner.invoke(main, ["skill", "refresh", "--sync-opencode-user-skills"])

        assert result.exit_code == 1
        assert "skill sync integrity check failed" in result.output
        assert "aa-case-design/SKILL.md" in result.output


def test_refresh_dry_run_writes_nothing(tmp_path: Path) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        Path(".aa").mkdir()
        Path(".aa/config.yaml").write_text("version: 1\n", encoding="utf-8")
        result = runner.invoke(main, ["skill", "refresh", "--dry-run"])
        assert result.exit_code == 0
        assert not Path("skills").exists()
