from pathlib import Path

import pytest

from scripts.migrate_skills import migrate, rewrite_text, target_name


def test_rewrite_text_rewrites_cli_config_and_skill_names() -> None:
    source = "aws run; .aws/config; AWS_HOME; aws-workflow; `aws`"
    assert rewrite_text(source) == "aa run; .aa/config; AA_HOME; aa-workflow; `aa`"


def test_target_name_preserves_non_prefixed_skill() -> None:
    assert target_name("aws-workflow") == "aa-workflow"
    assert target_name("writing-skills") == "writing-skills"


def test_migrate_refuses_to_overwrite_reviewed_destination(tmp_path: Path) -> None:
    destination = tmp_path / "resources"
    reviewed = destination / "skills/aa-workflow/SKILL.md"
    reviewed.parent.mkdir(parents=True)
    reviewed.write_text("reviewed", encoding="utf-8")
    with pytest.raises(RuntimeError, match="--force"):
        migrate(tmp_path / "source", destination)
