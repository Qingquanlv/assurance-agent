from __future__ import annotations

from graph_engine import ENGINE_API_VERSION
from graph_engine.plugin_api import (
    PluginContribution,
    PluginDescriptor,
    ProviderSource,
    RegistryPorts,
    SchemaContribution,
)

from agent_runtime_contracts.schema import canonical_json_bytes
from agent_runtime_opencode.handler import OpenCodeHandler


_SOURCE = ProviderSource(
    distribution="agent-runtime-opencode",
    version="0.1.0",
    entrypoint_group="graph_engine.plugins",
    entrypoint_name="opencode",
    entrypoint_value="agent_runtime_opencode.plugin:OpenCodePlugin",
    declaration_path="agent_runtime_opencode/plugin-declaration.json",
    import_roots=("",),
)
_REQUEST_SCHEMA = canonical_json_bytes(
    {
        "additionalProperties": False,
        "properties": {
            "instructions": {
                "items": {"additionalProperties": False, "type": "object"},
                "type": "array",
            },
            "schema_version": {"const": "1", "type": "string"},
        },
        "required": ["instructions", "schema_version"],
        "type": "object",
    }
)
_RESULT_SCHEMA = canonical_json_bytes(
    {
        "additionalProperties": False,
        "properties": {
            "adapter_id": {"minLength": 1, "type": "string"},
            "result_digest": {"maxLength": 64, "minLength": 64, "type": "string"},
            "schema_version": {"const": "1", "type": "string"},
            "structured_result": {"additionalProperties": False, "type": "object"},
        },
        "required": ["adapter_id", "result_digest", "schema_version", "structured_result"],
        "type": "object",
    }
)


class OpenCodePlugin:
    @staticmethod
    def descriptor() -> PluginDescriptor:
        return PluginDescriptor(
            schema_version="1",
            source=_SOURCE,
            plugin_id="runtime.opencode",
            plugin_version="0.1.0",
            engine_api=ENGINE_API_VERSION,
            task_handlers=("runtime.opencode.execute",),
            commit_validators=(),
            schemas=("runtime.opencode.request", "runtime.opencode.result"),
        )

    @staticmethod
    def contribute(ports: RegistryPorts) -> PluginContribution:
        if ports.engine_api != ENGINE_API_VERSION:
            raise ValueError(f"unsupported engine API: {ports.engine_api!r}")
        return PluginContribution(
            task_handlers={"runtime.opencode.execute": OpenCodeHandler()},
            schemas=(
                SchemaContribution("runtime.opencode.request", "application/schema+json", _REQUEST_SCHEMA),
                SchemaContribution("runtime.opencode.result", "application/schema+json", _RESULT_SCHEMA),
            ),
        )
