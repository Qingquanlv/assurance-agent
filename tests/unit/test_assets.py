from pathlib import Path

from assurance_agent.workflow.core.assets import (
    find_project_root,
    sync_opencode,
    sync_skills,
)


def _make_project(tmp_path: Path) -> Path:
    (tmp_path / ".aa").mkdir()
    (tmp_path / ".aa/config.yaml").write_text("version: 1\n", encoding="utf-8")
    return tmp_path


def test_find_project_root_by_config(tmp_path: Path) -> None:
    root = _make_project(tmp_path)
    nested = root / "a" / "b"
    nested.mkdir(parents=True)
    assert find_project_root(nested) == root


def test_find_project_root_by_qa_dir(tmp_path: Path) -> None:
    (tmp_path / "qa").mkdir()
    assert find_project_root(tmp_path) == tmp_path


def test_find_project_root_none(tmp_path: Path) -> None:
    assert find_project_root(tmp_path) is None


def test_sync_skills_creates_then_idempotent(tmp_path: Path) -> None:
    root = _make_project(tmp_path)
    first = sync_skills(root)
    assert (root / "skills" / "aa-workflow" / "SKILL.md").is_file()
    assert (root / "skills" / "writing-skills" / "SKILL.md").is_file()
    assert len(first.created) > 30
    assert first.updated == []

    second = sync_skills(root)
    assert second.created == []
    assert second.updated == []
    assert len(second.unchanged) == len(first.created)


def test_sync_skills_detects_update(tmp_path: Path) -> None:
    root = _make_project(tmp_path)
    sync_skills(root)
    target = root / "skills" / "aa-workflow" / "SKILL.md"
    target.write_text("stale", encoding="utf-8")
    result = sync_skills(root)
    assert "skills/aa-workflow/SKILL.md" in result.updated


def test_sync_skills_dry_run_writes_nothing(tmp_path: Path) -> None:
    root = _make_project(tmp_path)
    result = sync_skills(root, dry_run=True)
    assert len(result.created) > 30
    assert not (root / "skills").exists()


def test_sync_opencode_lays_down_agents_tools_plugin(tmp_path: Path) -> None:
    root = _make_project(tmp_path)
    result = sync_opencode(root)
    assert (root / ".opencode/agents/aa-doc-author.md").is_file()
    assert (root / ".opencode/tools/workflow_start.ts").is_file()
    assert (root / ".opencode/plugins/aa.mjs").is_file()
    assert any(p.startswith(".opencode/agents/") for p in result.created)
    assert sync_opencode(root).created == []
