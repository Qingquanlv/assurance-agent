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
    assert cfg.qa.archive == "./qa/archive"


def test_load_config_archive_defaults_when_omitted(tmp_path: Path) -> None:
    (tmp_path / ".aa").mkdir()
    (tmp_path / ".aa/config.yaml").write_text(
        """version: 1
sources: {frontend: ./frontend, backend: ./backend}
qa: {cases: ./qa/cases, changes: ./qa/changes}
tests: {root: ./tests, api: ./tests/api, e2e: ./tests/e2e}
frameworks:
  api: {enabled: true, name: pytest}
  e2e: {enabled: true, name: playwright}
generation: {prd_input_mode: prompt, e2e: {default_pom: false}}
execution: {entry: cli, self_healing: {mode: proposal-only}}
""",
        encoding="utf-8",
    )
    assert load_config(tmp_path).qa.archive == "./qa/archive"


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


def test_load_config_parses_strict_phase_model_routing(tmp_path: Path) -> None:
    write_default_config(tmp_path)
    path = tmp_path / ".aa/config.yaml"
    source = path.read_text(encoding="utf-8")
    routing = """
  model_routing:
    default: anthropic/deepseek-v4-flash
    strict_routes: true
    routes:
      aa-case-design: anthropic/glm-5.2
      aa-case-reviewer: anthropic/deepseek-v4-flash
    escalation:
      model: anthropic/glm-5.2
      on_error_kinds: [invalid_output, forbidden_write]
"""
    path.write_text(
        source.replace("\nreview:\n", f"\n{routing}\nreview:\n"),
        encoding="utf-8",
    )

    routing = load_config(tmp_path).execution.model_routing

    assert routing is not None
    assert routing.strict_routes is True
    assert routing.routes["aa-case-design"] == "anthropic/glm-5.2"


@pytest.mark.parametrize(
    "routing",
    [
        "routes: {aa-case-design: glm-5.2}",
        "routes: {'aa-*': anthropic/glm-5.2}",
        "strict_routes: 'true'",
        "unknown: value",
    ],
)
def test_load_config_rejects_invalid_phase_model_routing(tmp_path: Path, routing: str) -> None:
    write_default_config(tmp_path)
    path = tmp_path / ".aa/config.yaml"
    source = path.read_text(encoding="utf-8")
    path.write_text(
        source.replace("\nreview:\n", f"\n  model_routing:\n    {routing}\n\nreview:\n"),
        encoding="utf-8",
    )

    with pytest.raises(ConfigInvalidError, match="execution.model_routing"):
        load_config(tmp_path)
