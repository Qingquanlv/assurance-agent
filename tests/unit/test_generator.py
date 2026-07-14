import json
from pathlib import Path

import pytest
import yaml

from assurance_agent.config import ConfigNotFoundError
from assurance_agent.workflow.core.generator import GITKEEP_DIRS, generate_project, repair_project
from assurance_agent.workflow.core.templates import InitAnswers


def test_generate_project_writes_config_and_scaffold(tmp_path: Path) -> None:
    result = generate_project(tmp_path, InitAnswers())

    assert ".aa/config.yaml" in result.created
    assert yaml.safe_load((tmp_path / ".aa/config.yaml").read_text())["version"] == 1
    policy = json.loads((tmp_path / ".aa/execution-policy.json").read_text())
    assert policy["targets"] == ["api", "e2e"]
    assert (tmp_path / ".aa/module-map.yaml").is_file()
    assert (tmp_path / ".aa/data-knowledge.yaml").is_file()
    for rel in GITKEEP_DIRS:
        assert (tmp_path / rel / ".gitkeep").is_file(), rel
    assert result.skipped == []


def test_generate_project_never_overwrites_data_knowledge(tmp_path: Path) -> None:
    generate_project(tmp_path, InitAnswers())
    knowledge = tmp_path / ".aa/data-knowledge.yaml"
    knowledge.write_text("version: 1\naccounts: {admin: {}}\n", encoding="utf-8")

    result = generate_project(tmp_path, InitAnswers())

    assert ".aa/data-knowledge.yaml" in result.skipped
    assert "admin" in knowledge.read_text()


def test_repair_only_creates_missing(tmp_path: Path) -> None:
    generate_project(tmp_path, InitAnswers())
    config_before = (tmp_path / ".aa/config.yaml").read_text()
    (tmp_path / "qa/cases/.gitkeep").unlink()

    result = repair_project(tmp_path)

    assert "qa/cases/.gitkeep" in result.created
    assert (tmp_path / ".aa/config.yaml").read_text() == config_before


def test_repair_without_config_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigNotFoundError):
        repair_project(tmp_path)
