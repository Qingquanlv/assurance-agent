from __future__ import annotations

from graph_engine import ENGINE_API_VERSION
from graph_engine.plugin_api import (
    PluginContribution,
    PluginDescriptor,
    ProviderSource,
    RegistryPorts,
    ResourceContribution,
)

from agent_runtime_fixture import (
    INSTRUCTIONS_RESOURCE_ID,
    RESULT_SCHEMA_RESOURCE_ID,
    package_resource_bytes,
)

_SOURCE = ProviderSource(
    distribution="agent-runtime-fixture",
    version="1.0.0",
    entrypoint_group="graph_engine.plugins",
    entrypoint_name="fixture",
    entrypoint_value="agent_runtime_fixture.plugin:FixturePlugin",
    declaration_path="agent_runtime_fixture/plugin-declaration.json",
    import_roots=("",),
)


class FixturePlugin:
    @staticmethod
    def descriptor() -> PluginDescriptor:
        return PluginDescriptor(
            schema_version="1",
            source=_SOURCE,
            plugin_id="fixture.runtime",
            plugin_version="1.0.0",
            engine_api=ENGINE_API_VERSION,
            task_handlers=(),
            commit_validators=(),
            resources=(INSTRUCTIONS_RESOURCE_ID, RESULT_SCHEMA_RESOURCE_ID),
        )

    @staticmethod
    def contribute(ports: RegistryPorts) -> PluginContribution:
        if ports.engine_api != ENGINE_API_VERSION:
            raise ValueError(f"unsupported engine API: {ports.engine_api!r}")
        owned = package_resource_bytes()
        return PluginContribution(
            resources=(
                ResourceContribution(
                    INSTRUCTIONS_RESOURCE_ID,
                    "text/plain",
                    owned[INSTRUCTIONS_RESOURCE_ID],
                ),
                ResourceContribution(
                    RESULT_SCHEMA_RESOURCE_ID,
                    "application/json",
                    owned[RESULT_SCHEMA_RESOURCE_ID],
                ),
            ),
        )
