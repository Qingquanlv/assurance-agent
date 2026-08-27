from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from agent_runtime_contracts.schema import canonical_json_bytes
from agent_runtime_cursor.config import CursorAdapterConfig
from agent_runtime_cursor.plugin import CursorPlugin
from graph_engine import ENGINE_API_VERSION
from graph_engine.plugin_api import PluginDescriptor, RegistryPorts, validate_contribution


_REPO_ROOT = Path(__file__).resolve().parents[3]
_PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "agent_runtime_cursor"
_ENGINE_ROOT = _REPO_ROOT / "packages" / "graph-engine" / "graph_engine"
_OPENCODE_ROOT = _REPO_ROOT / "packages" / "agent-runtime-opencode" / "agent_runtime_opencode"
_SHA = "a" * 64
_CANARY = "canary-secret-value"


def _valid_config_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "1",
        "executable": "/opt/cursor/cursor",
        "executable_digest": _SHA,
        "expected_version": "1.0.0",
        "secret_handle": "cursor.api-key",
        "environment_names": ["PATH", "CURSOR_API_KEY"],
        "graceful_cancel_seconds": 5,
        "forced_cancel_seconds": 10,
        "max_output_bytes": 65536,
        "max_line_bytes": 4096,
        "protocol_profile": "confined_process",
        "adapter_configuration_digest": _SHA,
    }
    payload.update(overrides)
    return payload


def _valid_config(**overrides: object) -> CursorAdapterConfig:
    return CursorAdapterConfig.model_validate(_valid_config_payload(**overrides))


def test_cursor_config_rejects_ambient_auth_fallbacks_and_unknowns() -> None:
    with pytest.raises(ValidationError):
        CursorAdapterConfig.model_validate(
            {
                "executable": "cursor",
                "secret_handle": "cursor.api-key",
                "model_fallback": "auto",
                "default_model": "composer-2",
            }
        )


def test_cursor_config_accepts_closed_handle_only_projection() -> None:
    config = _valid_config()
    dumped = json.loads(canonical_json_bytes(config.model_dump(mode="json")).decode("utf-8"))
    assert dumped["secret_handle"] == "cursor.api-key"
    assert "secret" not in dumped
    assert _CANARY not in canonical_json_bytes(dumped).decode("utf-8")
    with pytest.raises(ValidationError, match="frozen"):
        config.secret_handle = "other.token"  # type: ignore[misc]


@pytest.mark.parametrize(
    "payload",
    [
        {"executable": "cursor"},
        {"executable": "./cursor"},
        {"executable": "../cursor"},
        {"executable": "/opt/cursor/cursor;id"},
        {"executable": "/opt/cursor/cursor|sh"},
        {"executable": "/opt/cursor/cursor && true"},
        {"executable": "/opt/cursor/cursor$(id)"},
        {"executable": "/opt/cursor/cursor`id`"},
        {"model_fallback": "auto"},
        {"default_model": "composer-2"},
        {"provider_default": "cursor"},
        {"profile": "confined_process"},
        {"secret": _CANARY},
        {"authorization": "Bearer sk-secret-canary"},
        {"max_output_bytes": 16_000_001},
        {"max_output_bytes": 0},
        {"max_line_bytes": 1_000_001},
        {"graceful_cancel_seconds": 60.1},
        {"forced_cancel_seconds": 0},
        {"environment_names": ["PATH", "HTTP_PROXY"]},
        {"environment_names": ["PATH", "PATH"]},
        {"secret_handle": "cursor api-key"},
        {"secret_handle": "cursor/api-key"},
        {"executable_digest": "abc"},
    ],
)
def test_cursor_config_rejects_shell_relative_unknowns_and_unbounded_values(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        CursorAdapterConfig.model_validate(_valid_config_payload(**payload))


def test_plugin_registers_only_execute_capability_and_request_result_schemas() -> None:
    plugin = CursorPlugin()
    descriptor = plugin.descriptor()
    contribution = plugin.contribute(RegistryPorts(engine_api=ENGINE_API_VERSION))
    validate_contribution(descriptor, contribution)
    declaration = json.loads((_PACKAGE_ROOT / "plugin-declaration.json").read_text(encoding="utf-8"))
    assert declaration["kind"] == "plugin"
    assert "manifest" not in declaration
    assert descriptor.plugin_id == "runtime.cursor"
    assert PluginDescriptor.model_validate(declaration["descriptor"]) == descriptor
    assert descriptor.task_handlers == ("runtime.cursor.execute",)
    assert set(descriptor.schemas) == {
        "runtime.cursor.request",
        "runtime.cursor.result",
    }
    assert descriptor.bindings == ()
    assert descriptor.effects == ()
    assert descriptor.commit_validators == ()
    assert descriptor.resources == ()
    assert tuple(contribution.task_handlers) == ("runtime.cursor.execute",)
    pyproject = (_PACKAGE_ROOT.parent / "pyproject.toml").read_text(encoding="utf-8")
    assert "graph_engine.products" not in pyproject
    assert "graph_engine.plugins" in pyproject
    root_pyproject = (_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    project_block = root_pyproject.split("[dependency-groups]", 1)[0]
    assert "agent-runtime-cursor" not in project_block
    assert "agent-runtime-cursor" in root_pyproject.split("[dependency-groups]", 1)[1]


def test_graph_engine_does_not_import_cursor_adapter() -> None:
    for path in sorted(_ENGINE_ROOT.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        assert "agent_runtime_cursor" not in text, path


def test_opencode_adapter_does_not_import_cursor_adapter() -> None:
    for path in sorted(_OPENCODE_ROOT.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        assert "agent_runtime_cursor" not in text, path


def _imported_modules(tree: ast.AST) -> tuple[str, ...]:
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.append(node.module)
    return tuple(names)


def test_adapter_source_does_not_import_runtime_peers_or_assurance() -> None:
    forbidden = (
        "graph_engine.runtime",
        "graph_engine.composition",
        "agent_runtime_opencode",
        "assurance_agent",
        "assurance_kernel",
    )
    for path in sorted(_PACKAGE_ROOT.rglob("*.py")):
        imported = _imported_modules(ast.parse(path.read_text(encoding="utf-8")))
        for module_name in imported:
            assert not any(
                module_name == prefix or module_name.startswith(prefix + ".") for prefix in forbidden
            ), (path, module_name)
