from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from agent_runtime_opencode.config import OpenCodeAdapterConfig
from agent_runtime_opencode.plugin import OpenCodePlugin
from agent_runtime_opencode.protocol import canonical_json_text
from graph_engine.plugin_api import PluginDescriptor, RegistryPorts, validate_contribution


_REPO_ROOT = Path(__file__).resolve().parents[3]
_PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "agent_runtime_opencode"
_ENGINE_ROOT = _REPO_ROOT / "packages" / "graph-engine" / "graph_engine"
_SHA = "a" * 64
_CANARY = "canary-secret-value"
_PINNED_CLIENT_ROUTES = frozenset(
    {
        "abort",
        "admit_message",
        "aclose",
        "create_session",
        "get_message",
        "get_profile",
        "get_server_identity",
        "get_session",
        "get_status",
        "list_sessions",
        "open_sse",
    }
)


def _valid_config_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "1",
        "endpoint": "http://127.0.0.1:4096",
        "tls_identity_digest": _SHA,
        "secret_handle": "opencode.token",
        "protocol_profile": "opencode-http-v1",
        "project_scope": "/tmp/attempt-workspace",
        "request_timeout_seconds": 30,
        "observation_horizon_seconds": 120,
        "max_response_bytes": 65536,
    }
    payload.update(overrides)
    return payload


def _valid_config(**overrides: object) -> OpenCodeAdapterConfig:
    return OpenCodeAdapterConfig.model_validate(_valid_config_payload(**overrides))


def test_opencode_config_rejects_ambient_auth_and_fallbacks() -> None:
    with pytest.raises(ValidationError):
        OpenCodeAdapterConfig.model_validate(
            {
                "endpoint": "http://127.0.0.1:4096",
                "secret_handle": "opencode.token",
                "profile": "opencode-http-v1",
                "model_fallback": "auto",
            }
        )


def test_opencode_config_accepts_closed_handle_only_projection() -> None:
    config = _valid_config()
    dumped = json.loads(canonical_json_text(config.model_dump(mode="json")))
    assert dumped["secret_handle"] == "opencode.token"
    assert "secret" not in dumped
    assert _CANARY not in canonical_json_text(dumped)
    with pytest.raises(ValidationError, match="frozen"):
        config.secret_handle = "other.token"  # type: ignore[misc]


@pytest.mark.parametrize(
    "payload",
    [
        {"endpoint": "http://user:pass@127.0.0.1:4096"},
        {"endpoint": "https://token:secret@example.invalid"},
        {"model_fallback": "auto"},
        {"default_model": "openai/gpt-4.1"},
        {"provider_default": "opencode"},
        {"profile": "opencode-http-v1"},
        {"max_response_bytes": 4_000_001},
        {"max_response_bytes": 0},
        {"request_timeout_seconds": 301},
        {"observation_horizon_seconds": 3600.1},
        {"secret": _CANARY},
        {"authorization": "Bearer sk-secret-canary"},
    ],
)
def test_opencode_config_rejects_credentials_defaults_unknowns_and_unbounded_values(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        OpenCodeAdapterConfig.model_validate(_valid_config_payload(**payload))


def test_plugin_registers_only_execute_capability_and_request_result_schemas() -> None:
    plugin = OpenCodePlugin()
    descriptor = plugin.descriptor()
    contribution = plugin.contribute(RegistryPorts(engine_api="1.0"))
    validate_contribution(descriptor, contribution)
    declaration = json.loads((_PACKAGE_ROOT / "plugin-declaration.json").read_text(encoding="utf-8"))
    assert declaration["kind"] == "plugin"
    assert "manifest" not in declaration
    assert descriptor.plugin_id == "runtime.opencode"
    assert PluginDescriptor.model_validate(declaration["descriptor"]) == descriptor
    assert descriptor.task_handlers == ("runtime.opencode.execute",)
    assert set(descriptor.schemas) == {
        "runtime.opencode.request",
        "runtime.opencode.result",
    }
    assert descriptor.bindings == ()
    assert descriptor.effects == ()
    assert descriptor.commit_validators == ()
    assert descriptor.resources == ()
    assert tuple(contribution.task_handlers) == ("runtime.opencode.execute",)
    pyproject = (_PACKAGE_ROOT.parent / "pyproject.toml").read_text(encoding="utf-8")
    assert "graph_engine.products" not in pyproject
    assert "graph_engine.plugins" in pyproject
    root_pyproject = (_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    project_block = root_pyproject.split("[dependency-groups]", 1)[0]
    assert "agent-runtime-opencode" not in project_block


def test_http_client_exposes_only_pinned_profile_routes() -> None:
    from agent_runtime_opencode.protocol import OpenCodeHttpClient

    public = {
        name
        for name, value in OpenCodeHttpClient.__dict__.items()
        if not name.startswith("_") and callable(value)
    }
    assert public <= _PINNED_CLIENT_ROUTES
    assert "request" not in public
    assert "get" not in public
    assert "post" not in public


def test_graph_engine_does_not_import_opencode_adapter() -> None:
    for path in sorted(_ENGINE_ROOT.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        assert "agent_runtime_opencode" not in text, path


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
        "agent_runtime_cursor",
        "assurance_agent",
        "assurance_kernel",
    )
    for path in sorted(_PACKAGE_ROOT.rglob("*.py")):
        imported = _imported_modules(ast.parse(path.read_text(encoding="utf-8")))
        for module_name in imported:
            assert not any(
                module_name == prefix or module_name.startswith(prefix + ".") for prefix in forbidden
            ), (path, module_name)
