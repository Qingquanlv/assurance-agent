from __future__ import annotations

from graph_engine import ENGINE_API_VERSION
from graph_engine.plugin_api import PluginDependency, ProviderSource
from graph_engine.plugin_kit import CapabilityPlugin, CapabilitySpec

from assurance_telemetry.resource_loader import resource_bytes

TELEMETRY_SOURCE = ProviderSource(
    distribution="assurance-telemetry",
    version="0.2.0",
    entrypoint_group="graph_engine.plugins",
    entrypoint_name="telemetry",
    entrypoint_value="assurance_telemetry.plugin:TelemetryPlugin",
    declaration_path="assurance_telemetry/plugin-declaration.json",
    import_roots=("",),
)

TELEMETRY_DEPENDENCIES: tuple[PluginDependency, ...] = (
    PluginDependency("assurance.intake", "==0.2.0"),
    PluginDependency("assurance.generation", "==0.2.0"),
)

_SCHEMA_FILES: dict[str, str] = {
    "assurance.telemetry.schema.telemetry-completion.v1": "schemas/telemetry-completion.v1.schema.json",
}


class TelemetryPlugin(CapabilityPlugin):
    spec = CapabilitySpec(
        plugin_id="assurance.telemetry",
        version="0.2.0",
        engine_api=ENGINE_API_VERSION,
        source=TELEMETRY_SOURCE,
        resource_bytes=resource_bytes,
        schema_files=_SCHEMA_FILES,
        resource_files={},
        task_handlers={},
        commit_validators={},
        dependencies=TELEMETRY_DEPENDENCIES,
    )
