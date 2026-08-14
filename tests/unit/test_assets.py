import json
import os
from pathlib import Path

import pytest

from assurance_agent import resources
from assurance_agent.exceptions import AaError
from assurance_agent.workflow.core import assets
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


def test_sync_skills_prunes_obsolete_aa_namespace_member(tmp_path: Path) -> None:
    root = _make_project(tmp_path)
    obsolete = root / "skills/aa-obsolete/SKILL.md"
    obsolete.parent.mkdir(parents=True)
    obsolete.write_text(
        "---\nname: aa-obsolete\ndescription: removed\n---\nobsolete\n",
        encoding="utf-8",
    )

    result = sync_skills(root)

    assert not obsolete.parent.exists()
    assert result.removed == ["skills/aa-obsolete"]
    assert assets.verify_packaged_skills(root / "skills") > 0


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
    assert (root / ".opencode/tools/artifact_write.ts").is_file()
    assert (root / ".opencode/plugins/aa.mjs").is_file()
    assert json.loads((root / ".opencode/oh-my-openagent.json").read_text()) == {
        "disabled_tools": ["apply_patch", "webfetch", "websearch", "websearch_web_search_exa"]
    }
    assert (root / ".opencode/skills/aa-case-design/SKILL.md").read_text(
        encoding="utf-8"
    ) == resources.read_text("skills", "aa-case-design", "SKILL.md")
    assert any(p.startswith(".opencode/agents/") for p in result.created)
    assert sync_opencode(root).created == []


def test_sync_opencode_preserves_existing_omo_config_when_disabling_apply_patch(
    tmp_path: Path,
) -> None:
    root = _make_project(tmp_path)
    config_path = root / ".opencode/oh-my-openagent.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        json.dumps({"disabled_tools": ["look_at"], "model_fallback": False}),
        encoding="utf-8",
    )

    result = sync_opencode(root)

    assert ".opencode/oh-my-openagent.json" in result.updated
    assert json.loads(config_path.read_text()) == {
        "disabled_tools": ["look_at", "apply_patch", "webfetch", "websearch", "websearch_web_search_exa"],
        "model_fallback": False,
    }


def test_sync_opencode_disables_builtin_apply_patch_in_existing_project_config(
    tmp_path: Path,
) -> None:
    root = _make_project(tmp_path)
    config_path = root / "opencode.json"
    config_path.write_text(
        json.dumps({"plugin": ["personal-plugin"], "tools": {"write": True}}),
        encoding="utf-8",
    )

    result = sync_opencode(root)

    assert "opencode.json" in result.updated
    assert json.loads(config_path.read_text()) == {
        "plugin": ["personal-plugin"],
        "tools": {
            "write": True,
            "apply_patch": False,
            "webfetch": False,
            "websearch": False,
            "websearch_web_search_exa": False,
        },
    }


@pytest.mark.parametrize(
    "agent_name",
    [
        "aa-archiver",
        "aa-explorer",
        "aa-intake-host",
        "aa-reporter",
        "aa-reviewer",
        "aa-test-author",
    ],
)
def test_sync_opencode_exposes_native_write_to_bounded_agents(
    tmp_path: Path,
    agent_name: str,
) -> None:
    root = _make_project(tmp_path)

    sync_opencode(root)

    agent = (root / ".opencode/agents" / f"{agent_name}.md").read_text(encoding="utf-8")
    frontmatter = agent.split("---", 2)[1]
    assert "  write: true\n" in frontmatter
    assert "  apply_patch: false\n" in frontmatter


def test_sync_opencode_exposes_native_authoring_tools_to_doc_author(tmp_path: Path) -> None:
    root = _make_project(tmp_path)

    sync_opencode(root)

    agent = (root / ".opencode/agents/aa-doc-author.md").read_text(encoding="utf-8")
    frontmatter = agent.split("---", 2)[1]
    assert "  write: true\n" in frontmatter
    assert "  apply_patch: false\n" in frontmatter
    assert '    "**qa/changes/**/cases/**": allow\n' in frontmatter


def test_sync_opencode_ignores_third_party_node_modules_symlinks(tmp_path: Path) -> None:
    root = _make_project(tmp_path)
    sync_opencode(root)
    package_binary = root / ".opencode/node_modules/.bin/tool"
    package_binary.parent.mkdir(parents=True)
    package_binary.symlink_to("../package/bin/tool")

    result = sync_opencode(root)

    assert result.created == []
    assert package_binary.is_symlink()


def test_sync_opencode_prunes_obsolete_aa_runtime_skill(tmp_path: Path) -> None:
    root = _make_project(tmp_path)
    obsolete = root / ".opencode/skills/aa-obsolete/SKILL.md"
    obsolete.parent.mkdir(parents=True)
    obsolete.write_text(
        "---\nname: aa-obsolete\ndescription: removed\n---\nobsolete\n",
        encoding="utf-8",
    )

    result = sync_opencode(root)

    assert not obsolete.parent.exists()
    assert result.removed == [".opencode/skills/aa-obsolete"]
    assert assets.verify_packaged_skills(root / ".opencode/skills") > 0


def test_verify_packaged_skills_rejects_stale_opencode_runtime_copy(tmp_path: Path) -> None:
    root = _make_project(tmp_path)
    sync_opencode(root)
    runtime_skills = root / ".opencode/skills"
    (runtime_skills / "aa-case-design/SKILL.md").write_text("stale", encoding="utf-8")

    with pytest.raises(AaError, match=r"aa-case-design/SKILL\.md.*sha256 mismatch"):
        assets.verify_packaged_skills(runtime_skills)


def test_verify_packaged_skills_rejects_extra_skill_shadowing_managed_name(tmp_path: Path) -> None:
    root = _make_project(tmp_path)
    sync_opencode(root)
    runtime_skills = root / ".opencode/skills"
    shadow = runtime_skills / "00-stale/SKILL.md"
    shadow.parent.mkdir()
    shadow.write_text(
        "---\nname: aa-case-design\ndescription: stale shadow\n---\nstale\n",
        encoding="utf-8",
    )

    with pytest.raises(AaError, match=r"00-stale/SKILL\.md.*shadows managed skill aa-case-design"):
        assets.verify_packaged_skills(runtime_skills)


def test_verify_packaged_skills_rejects_casefolded_managed_name_shadow(tmp_path: Path) -> None:
    root = _make_project(tmp_path)
    sync_opencode(root)
    runtime_skills = root / ".opencode/skills"
    shadow = runtime_skills / "00-stale/SKILL.md"
    shadow.parent.mkdir()
    shadow.write_text(
        "---\nname: AA-CASE-DESIGN\ndescription: stale shadow\n---\nstale\n",
        encoding="utf-8",
    )

    with pytest.raises(AaError, match=r"00-stale/SKILL\.md.*AA-CASE-DESIGN"):
        assets.verify_packaged_skills(runtime_skills)


def test_verify_packaged_skills_rejects_obsolete_aa_namespace_member(tmp_path: Path) -> None:
    root = _make_project(tmp_path)
    sync_opencode(root)
    runtime_skills = root / ".opencode/skills"
    obsolete = runtime_skills / "aa-obsolete/SKILL.md"
    obsolete.parent.mkdir()
    obsolete.write_text(
        "---\nname: aa-obsolete\ndescription: removed\n---\nobsolete\n",
        encoding="utf-8",
    )

    with pytest.raises(AaError, match=r"aa-obsolete.*unexpected AA skill"):
        assets.verify_packaged_skills(runtime_skills)


def test_verify_packaged_skills_rejects_shadow_nested_under_directory_symlink(tmp_path: Path) -> None:
    root = _make_project(tmp_path)
    sync_opencode(root)
    runtime_skills = root / ".opencode/skills"
    nested = tmp_path / "external-skill-tree/nested"
    nested.mkdir(parents=True)
    (nested / "SKILL.md").write_text(
        "---\nname: aa-case-design\ndescription: stale shadow\n---\nstale\n",
        encoding="utf-8",
    )
    os.symlink(nested.parent, runtime_skills / "00-stale", target_is_directory=True)

    with pytest.raises(AaError, match=r"00-stale.*symbolic link"):
        assets.verify_packaged_skills(runtime_skills)


def test_sync_opencode_rejects_file_symlink_without_overwriting_target(tmp_path: Path) -> None:
    root = _make_project(tmp_path)
    external = tmp_path / "external-skill.md"
    external.write_text("do not overwrite\n", encoding="utf-8")
    destination = root / ".opencode/skills/aa-case-design/SKILL.md"
    destination.parent.mkdir(parents=True)
    destination.symlink_to(external)

    with pytest.raises(AaError, match=r"aa-case-design/SKILL\.md.*symbolic link"):
        sync_opencode(root)

    assert external.read_text(encoding="utf-8") == "do not overwrite\n"


def test_verify_packaged_skills_rejects_expected_file_symlink(tmp_path: Path) -> None:
    root = _make_project(tmp_path)
    sync_opencode(root)
    runtime_skills = root / ".opencode/skills"
    external = tmp_path / "external-skill.md"
    external.write_text(
        resources.read_text("skills", "aa-case-design", "SKILL.md"),
        encoding="utf-8",
    )
    destination = runtime_skills / "aa-case-design/SKILL.md"
    destination.unlink()
    destination.symlink_to(external)

    with pytest.raises(AaError, match=r"aa-case-design/SKILL\.md.*symbolic link"):
        assets.verify_packaged_skills(runtime_skills)


def test_sync_opencode_rejects_symlinked_parent_without_writing_outside(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    root = _make_project(project)
    external = tmp_path / "external-opencode"
    external.mkdir()
    (root / ".opencode").symlink_to(external, target_is_directory=True)

    with pytest.raises(AaError, match=r"\.opencode.*symbolic link"):
        sync_opencode(root)

    assert list(external.iterdir()) == []


def test_user_skill_sync_rejects_third_party_symlink_without_following_or_deleting(
    tmp_path: Path,
) -> None:
    runtime_skills = tmp_path / "runtime-skills"
    runtime_skills.mkdir()
    external = tmp_path / "third-party-skill"
    external.mkdir()
    (external / "SKILL.md").write_text(
        "---\nname: aa-case-design\ndescription: stale shadow\n---\nexternal\n",
        encoding="utf-8",
    )
    third_party = runtime_skills / "third-party"
    third_party.symlink_to(external, target_is_directory=True)

    with pytest.raises(AaError, match=r"third-party.*symbolic link"):
        assets.sync_opencode_user_skills(runtime_skills)

    assert third_party.is_symlink()
    assert (external / "SKILL.md").read_text(encoding="utf-8").endswith("external\n")


def test_user_skill_sync_prunes_obsolete_regular_aa_namespace_member(tmp_path: Path) -> None:
    runtime_skills = tmp_path / "runtime-skills"
    obsolete = runtime_skills / "aa-obsolete/SKILL.md"
    obsolete.parent.mkdir(parents=True)
    obsolete.write_text(
        "---\nname: aa-obsolete\ndescription: removed\n---\nobsolete\n",
        encoding="utf-8",
    )

    result = assets.sync_opencode_user_skills(runtime_skills)

    assert not obsolete.parent.exists()
    assert result.removed == ["opencode-user-skills/aa-obsolete"]
    assert assets.verify_packaged_skills(runtime_skills, namespaced_only=True) > 0


def test_user_skill_sync_rejects_casefolded_managed_symlink_shadow(tmp_path: Path) -> None:
    runtime_skills = tmp_path / "runtime-skills"
    runtime_skills.mkdir()
    external = tmp_path / "stale-case-design"
    external.mkdir()
    shadow = runtime_skills / "AA-CASE-DESIGN"
    shadow.symlink_to(external, target_is_directory=True)

    with pytest.raises(AaError, match=r"AA-CASE-DESIGN.*symbolic link"):
        assets.sync_opencode_user_skills(runtime_skills)

    assert list(external.iterdir()) == []


def test_user_skill_sync_rejects_casefolded_shadow_in_regular_third_party_dir(
    tmp_path: Path,
) -> None:
    runtime_skills = tmp_path / "runtime-skills"
    shadow = runtime_skills / "third-party/SKILL.md"
    shadow.parent.mkdir(parents=True)
    shadow.write_text(
        "---\nname: AA-CASE-DESIGN\ndescription: stale\n---\nstale\n",
        encoding="utf-8",
    )

    with pytest.raises(AaError, match=r"third-party/SKILL\.md.*shadows managed skill"):
        assets.sync_opencode_user_skills(runtime_skills)


def test_user_agent_sync_updates_managed_agents_and_preserves_unrelated_agent(
    tmp_path: Path,
) -> None:
    runtime_agents = tmp_path / "agents"
    runtime_agents.mkdir()
    unrelated = runtime_agents / "personal-reviewer.md"
    unrelated.write_text(
        "---\nname: personal-reviewer\ndescription: personal\n---\nkeep\n",
        encoding="utf-8",
    )

    first = assets.sync_opencode_user_agents(runtime_agents)

    managed = runtime_agents / "aa-doc-author.md"
    assert managed.read_text(encoding="utf-8") == resources.read_text(
        "opencode", "agents", "aa-doc-author.md"
    )
    assert unrelated.read_text(encoding="utf-8").endswith("keep\n")
    assert "opencode-user-agents/aa-doc-author.md" in first.created

    managed.write_text("stale", encoding="utf-8")
    repaired = assets.sync_opencode_user_agents(runtime_agents)
    assert "opencode-user-agents/aa-doc-author.md" in repaired.updated
    assert assets.verify_packaged_agents(runtime_agents) > 0

    idempotent = assets.sync_opencode_user_agents(runtime_agents)
    assert idempotent.created == []
    assert idempotent.updated == []


def test_user_agent_sync_prunes_obsolete_regular_aa_agent(tmp_path: Path) -> None:
    runtime_agents = tmp_path / "agents"
    runtime_agents.mkdir()
    obsolete = runtime_agents / "aa-obsolete.md"
    obsolete.write_text(
        "---\nname: aa-obsolete\ndescription: obsolete\n---\nobsolete\n",
        encoding="utf-8",
    )

    result = assets.sync_opencode_user_agents(runtime_agents)

    assert not obsolete.exists()
    assert result.removed == ["opencode-user-agents/aa-obsolete.md"]


def test_user_agent_sync_rejects_symlink_without_touching_target(tmp_path: Path) -> None:
    runtime_agents = tmp_path / "agents"
    runtime_agents.mkdir()
    external = tmp_path / "external-agent.md"
    external.write_text("external\n", encoding="utf-8")
    shadow = runtime_agents / "aa-doc-author.md"
    shadow.symlink_to(external)

    with pytest.raises(AaError, match=r"aa-doc-author\.md.*symbolic link"):
        assets.sync_opencode_user_agents(runtime_agents)

    assert shadow.is_symlink()
    assert external.read_text(encoding="utf-8") == "external\n"


@pytest.mark.parametrize(
    ("filename", "frontmatter_name"),
    [
        ("AA-DOC-AUTHOR.md", "AA-DOC-AUTHOR"),
        ("personal.md", "aa-doc-author"),
    ],
)
def test_user_agent_sync_rejects_casefolded_or_frontmatter_shadow(
    tmp_path: Path,
    filename: str,
    frontmatter_name: str,
) -> None:
    runtime_agents = tmp_path / "agents"
    runtime_agents.mkdir()
    shadow = runtime_agents / filename
    shadow.write_text(
        f"---\nname: {frontmatter_name}\ndescription: stale\n---\nstale\n",
        encoding="utf-8",
    )

    with pytest.raises(AaError, match=r"shadows managed agent"):
        assets.sync_opencode_user_agents(runtime_agents)
