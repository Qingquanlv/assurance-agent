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

from assurance_improvement.resource_loader import resource_bytes

IMPROVEMENT_SOURCE = ProviderSource(
    distribution="assurance-improvement",
    version="0.1.0",
    entrypoint_group="graph_engine.plugins",
    entrypoint_name="improvement",
    entrypoint_value="assurance_improvement.plugin:ImprovementPlugin",
    declaration_path="assurance_improvement/plugin-declaration.json",
    import_roots=("",),
)

IMPROVEMENT_DEPENDENCIES: tuple[PluginDependency, ...] = (
    PluginDependency("assurance.intake", "==0.1.0"),
    PluginDependency("assurance.generation", "==0.1.0"),
    PluginDependency("assurance.execution", "==0.1.0"),
    PluginDependency("assurance.healing", "==0.1.0"),
    PluginDependency("assurance.quality", "==0.1.0"),
)

IMPROVEMENT_SCHEMA_IDS: tuple[str, ...] = (
    "assurance.improvement.schema.declaration-proposal.v1",
    "assurance.improvement.schema.improvement-candidates.v3",
    "assurance.improvement.schema.improvement-delivery.v1",
    "assurance.improvement.schema.improvement-effect-intent.v1",
    "assurance.improvement.schema.improvement-effect-receipt.v1",
    "assurance.improvement.schema.improvement-review.v1",
    "assurance.improvement.schema.promotion.v1",
    "assurance.improvement.schema.retro-context.v3",
    "assurance.improvement.schema.retro-signals.v3",
)

_SCHEMA_FILES: dict[str, str] = {
    "assurance.improvement.schema.declaration-proposal.v1": "schemas/declaration-proposal.v1.schema.json",
    "assurance.improvement.schema.improvement-candidates.v3": "schemas/improvement-candidates.v3.schema.json",
    "assurance.improvement.schema.improvement-delivery.v1": "schemas/improvement-delivery.v1.schema.json",
    "assurance.improvement.schema.improvement-effect-intent.v1": "schemas/improvement-effect-intent.v1.schema.json",
    "assurance.improvement.schema.improvement-effect-receipt.v1": "schemas/improvement-effect-receipt.v1.schema.json",
    "assurance.improvement.schema.improvement-review.v1": "schemas/improvement-review.v1.schema.json",
    "assurance.improvement.schema.promotion.v1": "schemas/promotion.v1.schema.json",
    "assurance.improvement.schema.retro-context.v3": "schemas/retro-context.v3.schema.json",
    "assurance.improvement.schema.retro-signals.v3": "schemas/retro-signals.v3.schema.json",
}


def _schema_contributions() -> tuple[SchemaContribution, ...]:
    return tuple(
        SchemaContribution(
            schema_id=schema_id,
            media_type="application/schema+json",
            content=resource_bytes(_SCHEMA_FILES[schema_id]),
        )
        for schema_id in IMPROVEMENT_SCHEMA_IDS
    )


class ImprovementPlugin:
    @staticmethod
    def descriptor() -> PluginDescriptor:
        return PluginDescriptor(
            schema_version="1",
            source=IMPROVEMENT_SOURCE,
            plugin_id="assurance.improvement",
            plugin_version="0.1.0",
            engine_api=ENGINE_API_VERSION,
            task_handlers=(),
            commit_validators=(),
            dependencies=IMPROVEMENT_DEPENDENCIES,
            schemas=IMPROVEMENT_SCHEMA_IDS,
            resources=(),
            effects=(),
            bindings=(),
        )

    @staticmethod
    def contribute(ports: RegistryPorts) -> PluginContribution:
        if ports.engine_api != ENGINE_API_VERSION:
            raise ValueError(f"unsupported engine API: {ports.engine_api!r}")
        return PluginContribution(schemas=_schema_contributions())
