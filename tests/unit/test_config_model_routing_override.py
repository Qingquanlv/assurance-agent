from pathlib import Path

import pytest

from assurance_agent.config import ConfigInvalidError, load_config
from assurance_agent.workflow.core.templates import InitAnswers, build_config_yaml


def _write_project(root: Path) -> None:
    (root / ".aa").mkdir()
    (root / ".aa/config.yaml").write_text(build_config_yaml(InitAnswers()), encoding="utf-8")


def test_model_routing_override_is_opt_in(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_project(tmp_path)
    monkeypatch.delenv("AA_MODEL_ROUTING_FILE", raising=False)

    assert load_config(tmp_path).execution.model_routing is None


def test_relative_model_routing_override_replaces_only_routing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_project(tmp_path)
    (tmp_path / "benchmark").mkdir()
    (tmp_path / "benchmark/openai-routing.yaml").write_text(
        """default: openai/gpt-5.6-luna
strict_routes: true
routes:
  aa-case-design: openai/gpt-5.6-terra
escalation:
  model: openai/gpt-5.6-terra
  on_error_kinds: [invalid_output, forbidden_write]
""",
        encoding="utf-8",
    )
    monkeypatch.delenv("AA_MODEL_ROUTING_FILE", raising=False)
    baseline = load_config(tmp_path)
    monkeypatch.setenv("AA_MODEL_ROUTING_FILE", "benchmark/openai-routing.yaml")

    config = load_config(tmp_path)

    assert config.execution.model_routing is not None
    assert config.execution.model_routing.default == "openai/gpt-5.6-luna"
    assert config.execution.model_routing.routes == {"aa-case-design": "openai/gpt-5.6-terra"}
    expected = baseline.model_dump()
    expected["execution"]["model_routing"] = config.execution.model_routing.model_dump()
    assert config.model_dump() == expected


def test_empty_model_routing_override_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_project(tmp_path)
    monkeypatch.setenv("AA_MODEL_ROUTING_FILE", "   ")

    with pytest.raises(ConfigInvalidError, match="AA_MODEL_ROUTING_FILE"):
        load_config(tmp_path)


def test_non_utf8_model_routing_override_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_project(tmp_path)
    (tmp_path / "invalid-utf8.yaml").write_bytes(b"\xff")
    monkeypatch.setenv("AA_MODEL_ROUTING_FILE", "invalid-utf8.yaml")

    with pytest.raises(ConfigInvalidError, match="AA_MODEL_ROUTING_FILE"):
        load_config(tmp_path)


@pytest.mark.parametrize(
    ("filename", "contents"),
    [
        ("missing.yaml", None),
        ("malformed.yaml", "routes: ["),
        ("invalid.yaml", "routes: {aa-case-design: gpt-5.6-terra}\n"),
    ],
)
def test_invalid_model_routing_override_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    filename: str,
    contents: str | None,
) -> None:
    _write_project(tmp_path)
    if contents is not None:
        (tmp_path / filename).write_text(contents, encoding="utf-8")
    monkeypatch.setenv("AA_MODEL_ROUTING_FILE", filename)

    with pytest.raises(ConfigInvalidError, match="AA_MODEL_ROUTING_FILE"):
        load_config(tmp_path)
