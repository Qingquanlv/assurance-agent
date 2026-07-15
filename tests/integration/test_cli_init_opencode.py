import json
from pathlib import Path

from click.testing import CliRunner

from assurance_agent.cli import main
from assurance_agent.workflow.core.assets import PLUGIN_ENTRY


def test_init_registers_opencode_fresh(tmp_path: Path) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(main, ["init", "--yes"])
        assert result.exit_code == 0, result.output
        assert "created: opencode.json" in result.output
        doc = json.loads(Path("opencode.json").read_text(encoding="utf-8"))
        assert doc["plugin"] == [PLUGIN_ENTRY]
        assert Path(".opencode/plugins/aa.mjs").is_file()
        assert Path("skills/aa-workflow/SKILL.md").is_file()


def test_init_merges_existing_opencode_json(tmp_path: Path) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        Path("opencode.json").write_text(
            json.dumps({"plugin": ["keep-me@2.0.0"], "model": "x"}, indent=2) + "\n",
            encoding="utf-8",
        )
        result = runner.invoke(main, ["init", "--yes"])
        assert result.exit_code == 0, result.output
        assert "updated: opencode.json" in result.output
        doc = json.loads(Path("opencode.json").read_text(encoding="utf-8"))
        assert doc["plugin"] == ["keep-me@2.0.0", PLUGIN_ENTRY]
        assert doc["model"] == "x"


def test_init_opencode_registration_idempotent(tmp_path: Path) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        runner.invoke(main, ["init", "--yes"])
        runner.invoke(main, ["init", "--yes"])
        doc = json.loads(Path("opencode.json").read_text(encoding="utf-8"))
        assert doc["plugin"].count(PLUGIN_ENTRY) == 1
