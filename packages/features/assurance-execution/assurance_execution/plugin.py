from __future__ import annotations

import json

from graph_engine import ENGINE_API_VERSION
from graph_engine.canonical import canonical_json_bytes
from graph_engine.plugin_api import (
    PluginContribution,
    PluginDependency,
    PluginDescriptor,
    ProviderSource,
    RegistryPorts,
    ResourceContribution,
    SchemaContribution,
)

from assurance_execution.operations import execution_handlers
from assurance_execution.resource_loader import resource_bytes
from assurance_execution.validators import ClosedMappingValidator, ExecutionEvidenceValidator

EXECUTION_SOURCE = ProviderSource(
    distribution="assurance-execution",
    version="0.2.0",
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
    "assurance.execution.workflow.execute.input.v1",
    "assurance.execution.workflow.execute.output.v1",
    "assurance.execution.workflow.rerun.input.v1",
    "assurance.execution.workflow.rerun.output.v1",
)

EXECUTION_DEPENDENCIES: tuple[PluginDependency, ...] = (
    PluginDependency("assurance.intake", "==0.2.0"),
    PluginDependency("assurance.generation", "==0.2.0"),
)

EXECUTION_HANDLER_IDS: tuple[str, ...] = (
    "assurance.execution.execute.finalize",
    "assurance.execution.execute.prepare",
    "assurance.execution.normalize",
    "assurance.execution.run-tests",
    "assurance.execution.run-tests-and-collect-pr-metrics",
    "assurance.execution.run.finalize",
    "assurance.execution.run.prepare",
    "assurance.execution.select",
)

EXECUTION_VALIDATOR_IDS: tuple[str, ...] = (
    "assurance.execution.validator.closed-mapping.v1",
    "assurance.execution.validator.evidence.v1",
)

EXECUTION_RESOURCE_FILES: dict[str, str] = {
    "assurance.execution.persona.executor.v1": "personas/executor.md",
    "assurance.execution.result.execution.v1": "result-contracts/execution.v1.schema.json",
    "assurance.execution.skill.aa-execute.v1": "skills/aa-execute/SKILL.md",
    "assurance.execution.skill.aa-run.v1": "skills/aa-run/SKILL.md",
    "assurance.execution.workflow.module.v1": "workflow/module.yaml",
}

EXECUTION_RESOURCE_IDS: tuple[str, ...] = tuple(sorted(EXECUTION_RESOURCE_FILES))

_SCHEMA_FILES: dict[str, str] = {
    "assurance.execution.schema.closed-mapping.v1": "schemas/closed-mapping.v1.schema.json",
    "assurance.execution.schema.execution-evidence.v1": "schemas/execution-evidence.v1.schema.json",
    "assurance.execution.schema.execution-manifest.v1": "schemas/execution-manifest.v1.schema.json",
    "assurance.execution.schema.selected-targets.v1": "schemas/selected-targets.v1.schema.json",
    "assurance.execution.workflow.execute.input.v1": "schemas/workflow/execute-input.v1.schema.json",
    "assurance.execution.workflow.execute.output.v1": "schemas/workflow/execute-output.v1.schema.json",
    "assurance.execution.workflow.rerun.input.v1": "schemas/workflow/rerun-input.v1.schema.json",
    "assurance.execution.workflow.rerun.output.v1": "schemas/workflow/rerun-output.v1.schema.json",
}

_VALIDATORS = {
    "assurance.execution.validator.closed-mapping.v1": ClosedMappingValidator(require_mapping=False),
    "assurance.execution.validator.evidence.v1": ExecutionEvidenceValidator(require_mapping=False),
}


def _schema_contributions() -> tuple[SchemaContribution, ...]:
    return tuple(
        SchemaContribution(
            schema_id=schema_id,
            media_type="application/schema+json",
            content=canonical_json_bytes(json.loads(resource_bytes(_SCHEMA_FILES[schema_id]))),
        )
        for schema_id in EXECUTION_SCHEMA_IDS
    )


_WORKFLOW_MODULE_MIME = "application/vnd.graph-engine.workflow-module+yaml"


def _resource_media_type(path: str) -> str:
    if path.endswith(".schema.json"):
        return "application/schema+json"
    if path.endswith("workflow/module.yaml"):
        return _WORKFLOW_MODULE_MIME
    return "text/plain"


def _resource_contributions() -> tuple[ResourceContribution, ...]:
    return tuple(
        ResourceContribution(
            resource_id=resource_id,
            media_type=_resource_media_type(EXECUTION_RESOURCE_FILES[resource_id]),
            content=resource_bytes(EXECUTION_RESOURCE_FILES[resource_id]),
        )
        for resource_id in EXECUTION_RESOURCE_IDS
    )


class ExecutionPlugin:
    @staticmethod
    def descriptor() -> PluginDescriptor:
        return PluginDescriptor(
            schema_version="1",
            source=EXECUTION_SOURCE,
            plugin_id="assurance.execution",
            plugin_version="0.2.0",
            engine_api=ENGINE_API_VERSION,
            task_handlers=EXECUTION_HANDLER_IDS,
            commit_validators=EXECUTION_VALIDATOR_IDS,
            dependencies=EXECUTION_DEPENDENCIES,
            schemas=EXECUTION_SCHEMA_IDS,
            resources=EXECUTION_RESOURCE_IDS,
        )

    @staticmethod
    def contribute(ports: RegistryPorts) -> PluginContribution:
        if ports.engine_api != ENGINE_API_VERSION:
            raise ValueError(f"unsupported engine API: {ports.engine_api!r}")
        return PluginContribution(
            task_handlers=execution_handlers(),
            commit_validators=_VALIDATORS,
            schemas=_schema_contributions(),
            resources=_resource_contributions(),
        )
