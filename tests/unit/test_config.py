from pathlib import Path

import pytest

from assurance_agent.config import ConfigInvalidError, ConfigNotFoundError, load_config
from assurance_agent.workflow.core.templates import InitAnswers, build_config_yaml


def write_default_config(root: Path) -> None:
    (root / ".aa").mkdir()
    (root / ".aa/config.yaml").write_text(build_config_yaml(InitAnswers()), encoding="utf-8")


def test_load_config_parses_generated_template(tmp_path: Path) -> None:
    write_default_config(tmp_path)
    cfg = load_config(tmp_path)
    assert cfg.sources.frontend == "./frontend"
    assert cfg.frameworks.api.enabled is True
    assert cfg.generation.prd_input_mode == "prompt"
    assert cfg.execution.entry == "cli"
    assert cfg.execution.self_healing.mode == "proposal-only"
    assert cfg.generation.e2e.default_pom is False


def test_load_config_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigNotFoundError):
        load_config(tmp_path)


def test_load_config_invalid_yaml_raises(tmp_path: Path) -> None:
    (tmp_path / ".aa").mkdir()
    (tmp_path / ".aa/config.yaml").write_text("frameworks: [not-a-mapping", encoding="utf-8")
    with pytest.raises(ConfigInvalidError):
        load_config(tmp_path)


def test_load_config_schema_violation_raises(tmp_path: Path) -> None:
    (tmp_path / ".aa").mkdir()
    (tmp_path / ".aa/config.yaml").write_text(
        "version: 1\nsources: {frontend: 1, backend: ./b}\n", encoding="utf-8"
    )
    with pytest.raises(ConfigInvalidError):
        load_config(tmp_path)
