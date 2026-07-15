import json
from pathlib import Path

from assurance_agent.workflow.core.assets import PLUGIN_ENTRY, register_opencode


def _project(tmp_path: Path) -> Path:
    (tmp_path / ".aa").mkdir()
    (tmp_path / ".aa/config.yaml").write_text("version: 1\n", encoding="utf-8")
    return tmp_path


def test_register_creates_opencode_json_and_assets(tmp_path: Path) -> None:
    root = _project(tmp_path)
    result = register_opencode(root)
    assert result.opencode_json_created is True
    doc = json.loads((root / "opencode.json").read_text(encoding="utf-8"))
    assert doc["plugin"] == [PLUGIN_ENTRY]
    assert (root / ".opencode/plugins/aa.mjs").is_file()
    assert (root / "skills/aa-workflow/SKILL.md").is_file()


def test_register_merges_into_existing_plugins(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "opencode.json").write_text(
        json.dumps({"plugin": ["other-plugin@1.0.0"], "theme": "dark"}, indent=2) + "\n",
        encoding="utf-8",
    )
    result = register_opencode(root)
    assert result.opencode_json_created is False
    doc = json.loads((root / "opencode.json").read_text(encoding="utf-8"))
    assert doc["plugin"] == ["other-plugin@1.0.0", PLUGIN_ENTRY]
    assert doc["theme"] == "dark"


def test_register_is_idempotent(tmp_path: Path) -> None:
    root = _project(tmp_path)
    register_opencode(root)
    register_opencode(root)
    doc = json.loads((root / "opencode.json").read_text(encoding="utf-8"))
    assert doc["plugin"].count(PLUGIN_ENTRY) == 1
