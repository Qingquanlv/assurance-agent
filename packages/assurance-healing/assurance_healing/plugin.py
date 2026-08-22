from __future__ import annotations

from graph_engine import ENGINE_API_VERSION
from graph_engine.plugin_api import (
    PluginContribution,
    PluginDependency,
    PluginDescriptor,
    ProviderSource,
    RegistryPorts,
    ResourceContribution,
    SchemaContribution,
)

from assurance_healing.resource_loader import resource_bytes

HEALING_SOURCE = ProviderSource(
    distribution="assurance-healing",
    version="0.1.0",
    entrypoint_group="graph_engine.plugins",
    entrypoint_name="healing",
    entrypoint_value="assurance_healing.plugin:HealingPlugin",
    declaration_path="assurance_healing/plugin-declaration.json",
    import_roots=("",),
)

HEALING_SCHEMA_IDS: tuple[str, ...] = (
    "assurance.healing.schema.allocation-intent.v2",
    "assurance.healing.schema.allocation-receipt.v2",
    "assurance.healing.schema.coverage-repair.v1",
    "assurance.healing.schema.fix-proposal.v1",
    "assurance.healing.schema.heal-apply-intent.v2",
    "assurance.healing.schema.heal-apply-receipt.v2",
    "assurance.healing.schema.healing-safety.v1",
    "assurance.healing.schema.healing-status.v1",
    "assurance.healing.schema.proposal-approved-intent.v1",
    "assurance.healing.schema.proposal-approved-receipt.v1",
)

HEALING_DEPENDENCIES: tuple[PluginDependency, ...] = (
    PluginDependency("assurance.intake", "==0.1.0"),
    PluginDependency("assurance.generation", "==0.1.0"),
    PluginDependency("assurance.execution", "==0.1.0"),
)

HEALING_RESOURCE_FILES: dict[str, str] = {
    "assurance.healing.policy.test-change-policy.v1": "policy/test-change-policy.v1.json",
}

HEALING_RESOURCE_IDS: tuple[str, ...] = tuple(sorted(HEALING_RESOURCE_FILES))

_SCHEMA_FILES: dict[str, str] = {
    "assurance.healing.schema.allocation-intent.v2": "schemas/allocation-intent.v2.schema.json",
    "assurance.healing.schema.allocation-receipt.v2": "schemas/allocation-receipt.v2.schema.json",
    "assurance.healing.schema.coverage-repair.v1": "schemas/coverage-repair.v1.schema.json",
    "assurance.healing.schema.fix-proposal.v1": "schemas/fix-proposal.v1.schema.json",
    "assurance.healing.schema.heal-apply-intent.v2": "schemas/heal-apply-intent.v2.schema.json",
    "assurance.healing.schema.heal-apply-receipt.v2": "schemas/heal-apply-receipt.v2.schema.json",
    "assurance.healing.schema.healing-safety.v1": "schemas/healing-safety.v1.schema.json",
    "assurance.healing.schema.healing-status.v1": "schemas/healing-status.v1.schema.json",
    "assurance.healing.schema.proposal-approved-intent.v1": (
        "schemas/proposal-approved-intent.v1.schema.json"
    ),
    "assurance.healing.schema.proposal-approved-receipt.v1": (
        "schemas/proposal-approved-receipt.v1.schema.json"
    ),
}


def _schema_contributions() -> tuple[SchemaContribution, ...]:
    return tuple(
        SchemaContribution(
            schema_id=schema_id,
            media_type="application/schema+json",
            content=resource_bytes(_SCHEMA_FILES[schema_id]),
        )
        for schema_id in HEALING_SCHEMA_IDS
    )


def _resource_media_type(path: str) -> str:
    if path.endswith(".schema.json"):
        return "application/schema+json"
    if path.endswith(".json"):
        return "application/json"
    return "text/plain"


def _resource_contributions() -> tuple[ResourceContribution, ...]:
    return tuple(
        ResourceContribution(
            resource_id=resource_id,
            media_type=_resource_media_type(HEALING_RESOURCE_FILES[resource_id]),
            content=resource_bytes(HEALING_RESOURCE_FILES[resource_id]),
        )
        for resource_id in HEALING_RESOURCE_IDS
    )


class HealingPlugin:
    @staticmethod
    def descriptor() -> PluginDescriptor:
        return PluginDescriptor(
            schema_version="1",
            source=HEALING_SOURCE,
            plugin_id="assurance.healing",
            plugin_version="0.1.0",
            engine_api=ENGINE_API_VERSION,
            task_handlers=(),
            commit_validators=(),
            dependencies=HEALING_DEPENDENCIES,
            schemas=HEALING_SCHEMA_IDS,
            resources=HEALING_RESOURCE_IDS,
        )

    @staticmethod
    def contribute(ports: RegistryPorts) -> PluginContribution:
        if ports.engine_api != ENGINE_API_VERSION:
            raise ValueError(f"unsupported engine API: {ports.engine_api!r}")
        return PluginContribution(
            schemas=_schema_contributions(),
            resources=_resource_contributions(),
        )
