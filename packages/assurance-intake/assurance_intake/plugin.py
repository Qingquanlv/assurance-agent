from __future__ import annotations

from graph_engine import ENGINE_API_VERSION
from graph_engine.plugin_api import (
    PluginContribution,
    PluginDescriptor,
    ProviderSource,
    RegistryPorts,
    SchemaContribution,
)

from assurance_intake.resource_loader import resource_bytes

INTAKE_SOURCE = ProviderSource(
    distribution="assurance-intake",
    version="0.1.0",
    entrypoint_group="graph_engine.plugins",
    entrypoint_name="intake",
    entrypoint_value="assurance_intake.plugin:IntakePlugin",
    declaration_path="assurance_intake/plugin-declaration.json",
    import_roots=("",),
)

INTAKE_SCHEMA_IDS: tuple[str, ...] = (
    "assurance.intake.schema.case-authoring.v1",
    "assurance.intake.schema.case-review.v1",
    "assurance.intake.schema.case.v1",
    "assurance.intake.schema.qa-change.v1",
)

_SCHEMA_FILES: dict[str, str] = {
    "assurance.intake.schema.case-authoring.v1": "schemas/case-authoring.v1.schema.json",
    "assurance.intake.schema.case-review.v1": "schemas/case-review.v1.schema.json",
    "assurance.intake.schema.case.v1": "schemas/case.v1.schema.json",
    "assurance.intake.schema.qa-change.v1": "schemas/qa-change.v1.schema.json",
}


def _schema_contributions() -> tuple[SchemaContribution, ...]:
    return tuple(
        SchemaContribution(
            schema_id=schema_id,
            media_type="application/schema+json",
            content=resource_bytes(_SCHEMA_FILES[schema_id]),
        )
        for schema_id in INTAKE_SCHEMA_IDS
    )


class IntakePlugin:
    @staticmethod
    def descriptor() -> PluginDescriptor:
        return PluginDescriptor(
            schema_version="1",
            source=INTAKE_SOURCE,
            plugin_id="assurance.intake",
            plugin_version="0.1.0",
            engine_api=ENGINE_API_VERSION,
            task_handlers=(),
            commit_validators=(),
            schemas=INTAKE_SCHEMA_IDS,
            resources=(),
        )

    @staticmethod
    def contribute(ports: RegistryPorts) -> PluginContribution:
        if ports.engine_api != ENGINE_API_VERSION:
            raise ValueError(f"unsupported engine API: {ports.engine_api!r}")
        return PluginContribution(schemas=_schema_contributions())
