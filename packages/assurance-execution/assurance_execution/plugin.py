from __future__ import annotations

from graph_engine import ENGINE_API_VERSION
from graph_engine.plugin_api import (
    PluginContribution,
    PluginDependency,
    PluginDescriptor,
    ProviderSource,
    RegistryPorts,
    SchemaContribution,
)

from assurance_execution.resource_loader import resource_bytes

EXECUTION_SOURCE = ProviderSource(
    distribution="assurance-execution",
    version="0.1.0",
    entrypoint_group="graph_engine.plugins",
    entrypoint_name="execution",
    entrypoint_value="assurance_execution.plugin:ExecutionPlugin",
    declaration_path="assurance_execution/plugin-declaration.json",
    import_roots=("",),
)

EXECUTION_SCHEMA_IDS: tuple[str, ...] = (
    "assurance.execution.schema.closed-mapping.v1",
    "assurance.execution.schema.execution-evidence.v1",
    "assurance.execution.schema.execution-manifest.v1",
    "assurance.execution.schema.selected-targets.v1",
)

EXECUTION_DEPENDENCIES: tuple[PluginDependency, ...] = (
    PluginDependency("assurance.intake", "==0.1.0"),
    PluginDependency("assurance.generation", "==0.1.0"),
)

_SCHEMA_FILES: dict[str, str] = {
    "assurance.execution.schema.closed-mapping.v1": "schemas/closed-mapping.v1.schema.json",
    "assurance.execution.schema.execution-evidence.v1": "schemas/execution-evidence.v1.schema.json",
    "assurance.execution.schema.execution-manifest.v1": "schemas/execution-manifest.v1.schema.json",
    "assurance.execution.schema.selected-targets.v1": "schemas/selected-targets.v1.schema.json",
}


def _schema_contributions() -> tuple[SchemaContribution, ...]:
    return tuple(
        SchemaContribution(
            schema_id=schema_id,
            media_type="application/schema+json",
            content=resource_bytes(_SCHEMA_FILES[schema_id]),
        )
        for schema_id in EXECUTION_SCHEMA_IDS
    )


class ExecutionPlugin:
    @staticmethod
    def descriptor() -> PluginDescriptor:
        return PluginDescriptor(
            schema_version="1",
            source=EXECUTION_SOURCE,
            plugin_id="assurance.execution",
            plugin_version="0.1.0",
            engine_api=ENGINE_API_VERSION,
            task_handlers=(),
            commit_validators=(),
            dependencies=EXECUTION_DEPENDENCIES,
            schemas=EXECUTION_SCHEMA_IDS,
            resources=(),
        )

    @staticmethod
    def contribute(ports: RegistryPorts) -> PluginContribution:
        if ports.engine_api != ENGINE_API_VERSION:
            raise ValueError(f"unsupported engine API: {ports.engine_api!r}")
        return PluginContribution(schemas=_schema_contributions())
