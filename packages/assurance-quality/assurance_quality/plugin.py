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

from assurance_quality.resource_loader import resource_bytes

QUALITY_SOURCE = ProviderSource(
    distribution="assurance-quality",
    version="0.1.0",
    entrypoint_group="graph_engine.plugins",
    entrypoint_name="quality",
    entrypoint_value="assurance_quality.plugin:QualityPlugin",
    declaration_path="assurance_quality/plugin-declaration.json",
    import_roots=("",),
)

QUALITY_DEPENDENCIES: tuple[PluginDependency, ...] = (
    PluginDependency("assurance.intake", "==0.1.0"),
    PluginDependency("assurance.generation", "==0.1.0"),
    PluginDependency("assurance.execution", "==0.1.0"),
    PluginDependency("assurance.healing", "==0.1.0"),
)

QUALITY_SCHEMA_IDS: tuple[str, ...] = (
    "assurance.quality.schema.adversarial-yield.v1",
    "assurance.quality.schema.assertion-strength.v1",
    "assurance.quality.schema.auth-matrix.v1",
    "assurance.quality.schema.baseline-drift.v1",
    "assurance.quality.schema.c-layer.v1",
    "assurance.quality.schema.constraint-coverage.v1",
    "assurance.quality.schema.coverage-diff.v1",
    "assurance.quality.schema.coverage-gaps.v1",
    "assurance.quality.schema.fact-baseline.v1",
    "assurance.quality.schema.issue-events.v1",
    "assurance.quality.schema.issues.v1",
    "assurance.quality.schema.journey-coverage.v1",
    "assurance.quality.schema.metrics.v1",
    "assurance.quality.schema.minimum-coverage.v1",
    "assurance.quality.schema.mutation.v1",
    "assurance.quality.schema.perf-slack.v1",
    "assurance.quality.schema.quality-gate.v2",
    "assurance.quality.schema.quarantine.v1",
    "assurance.quality.schema.report.v1",
    "assurance.quality.schema.sufficiency.v2",
    "assurance.quality.schema.trace-sufficiency.v1",
    "assurance.quality.schema.trace.v2",
)

_SCHEMA_FILES: dict[str, str] = {
    "assurance.quality.schema.adversarial-yield.v1": "schemas/adversarial-yield.v1.schema.json",
    "assurance.quality.schema.assertion-strength.v1": "schemas/assertion-strength.v1.schema.json",
    "assurance.quality.schema.auth-matrix.v1": "schemas/auth-matrix.v1.schema.json",
    "assurance.quality.schema.baseline-drift.v1": "schemas/baseline-drift.v1.schema.json",
    "assurance.quality.schema.c-layer.v1": "schemas/c-layer.v1.schema.json",
    "assurance.quality.schema.constraint-coverage.v1": "schemas/constraint-coverage.v1.schema.json",
    "assurance.quality.schema.coverage-diff.v1": "schemas/coverage-diff.v1.schema.json",
    "assurance.quality.schema.coverage-gaps.v1": "schemas/coverage-gaps.v1.schema.json",
    "assurance.quality.schema.fact-baseline.v1": "schemas/fact-baseline.v1.schema.json",
    "assurance.quality.schema.issue-events.v1": "schemas/issue-events.v1.schema.json",
    "assurance.quality.schema.issues.v1": "schemas/issues.v1.schema.json",
    "assurance.quality.schema.journey-coverage.v1": "schemas/journey-coverage.v1.schema.json",
    "assurance.quality.schema.metrics.v1": "schemas/metrics.v1.schema.json",
    "assurance.quality.schema.minimum-coverage.v1": "schemas/minimum-coverage.v1.schema.json",
    "assurance.quality.schema.mutation.v1": "schemas/mutation.v1.schema.json",
    "assurance.quality.schema.perf-slack.v1": "schemas/perf-slack.v1.schema.json",
    "assurance.quality.schema.quality-gate.v2": "schemas/quality-gate.v2.schema.json",
    "assurance.quality.schema.quarantine.v1": "schemas/quarantine.v1.schema.json",
    "assurance.quality.schema.report.v1": "schemas/report.v1.schema.json",
    "assurance.quality.schema.sufficiency.v2": "schemas/sufficiency.v2.schema.json",
    "assurance.quality.schema.trace-sufficiency.v1": "schemas/trace-sufficiency.v1.schema.json",
    "assurance.quality.schema.trace.v2": "schemas/trace.v2.schema.json",
}


def _schema_contributions() -> tuple[SchemaContribution, ...]:
    return tuple(
        SchemaContribution(
            schema_id=schema_id,
            media_type="application/schema+json",
            content=resource_bytes(_SCHEMA_FILES[schema_id]),
        )
        for schema_id in QUALITY_SCHEMA_IDS
    )


class QualityPlugin:
    @staticmethod
    def descriptor() -> PluginDescriptor:
        return PluginDescriptor(
            schema_version="1",
            source=QUALITY_SOURCE,
            plugin_id="assurance.quality",
            plugin_version="0.1.0",
            engine_api=ENGINE_API_VERSION,
            task_handlers=(),
            commit_validators=(),
            dependencies=QUALITY_DEPENDENCIES,
            schemas=QUALITY_SCHEMA_IDS,
            resources=(),
            effects=(),
            bindings=(),
        )

    @staticmethod
    def contribute(ports: RegistryPorts) -> PluginContribution:
        if ports.engine_api != ENGINE_API_VERSION:
            raise ValueError(f"unsupported engine API: {ports.engine_api!r}")
        return PluginContribution(schemas=_schema_contributions())
