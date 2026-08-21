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

from assurance_generation.resource_loader import resource_bytes

GENERATION_SOURCE = ProviderSource(
    distribution="assurance-generation",
    version="0.1.0",
    entrypoint_group="graph_engine.plugins",
    entrypoint_name="generation",
    entrypoint_value="assurance_generation.plugin:GenerationPlugin",
    declaration_path="assurance_generation/plugin-declaration.json",
    import_roots=("",),
)

GENERATION_SCHEMA_IDS: tuple[str, ...] = (
    "assurance.generation.schema.codegen-mapping.v1",
    "assurance.generation.schema.discovery-campaign.v1",
    "assurance.generation.schema.generated-files.v1",
    "assurance.generation.schema.plan-check.v1",
    "assurance.generation.schema.plan-review.v1",
)

GENERATION_DEPENDENCIES: tuple[PluginDependency, ...] = (PluginDependency("assurance.intake", "==0.1.0"),)

_SCHEMA_FILES: dict[str, str] = {
    "assurance.generation.schema.codegen-mapping.v1": "schemas/codegen-mapping.v1.schema.json",
    "assurance.generation.schema.discovery-campaign.v1": "schemas/discovery-campaign.v1.schema.json",
    "assurance.generation.schema.generated-files.v1": "schemas/generated-files.v1.schema.json",
    "assurance.generation.schema.plan-check.v1": "schemas/plan-check.v1.schema.json",
    "assurance.generation.schema.plan-review.v1": "schemas/plan-review.v1.schema.json",
}


def _schema_contributions() -> tuple[SchemaContribution, ...]:
    return tuple(
        SchemaContribution(
            schema_id=schema_id,
            media_type="application/schema+json",
            content=resource_bytes(_SCHEMA_FILES[schema_id]),
        )
        for schema_id in GENERATION_SCHEMA_IDS
    )


class GenerationPlugin:
    @staticmethod
    def descriptor() -> PluginDescriptor:
        return PluginDescriptor(
            schema_version="1",
            source=GENERATION_SOURCE,
            plugin_id="assurance.generation",
            plugin_version="0.1.0",
            engine_api=ENGINE_API_VERSION,
            task_handlers=(),
            commit_validators=(),
            dependencies=GENERATION_DEPENDENCIES,
            schemas=GENERATION_SCHEMA_IDS,
            resources=(),
        )

    @staticmethod
    def contribute(ports: RegistryPorts) -> PluginContribution:
        if ports.engine_api != ENGINE_API_VERSION:
            raise ValueError(f"unsupported engine API: {ports.engine_api!r}")
        return PluginContribution(schemas=_schema_contributions())
