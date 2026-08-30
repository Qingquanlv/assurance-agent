from __future__ import annotations

from graph_engine import ENGINE_API_VERSION
from graph_engine.plugin_api import PluginDependency, ProviderSource
from graph_engine.plugin_kit import CapabilityPlugin, CapabilitySpec

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

EXECUTION_DEPENDENCIES: tuple[PluginDependency, ...] = (
    PluginDependency("assurance.intake", "==0.2.0"),
    PluginDependency("assurance.generation", "==0.2.0"),
)

EXECUTION_RESOURCE_FILES: dict[str, str] = {
    "assurance.execution.persona.executor.v1": "personas/executor.md",
    "assurance.execution.result.execution.v1": "result-contracts/execution.v1.schema.json",
    "assurance.execution.skill.aa-execute.v1": "skills/aa-execute/SKILL.md",
    "assurance.execution.skill.aa-run.v1": "skills/aa-run/SKILL.md",
    "assurance.execution.workflow.module.v1": "workflow/module.yaml",
}

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

_HANDLERS = execution_handlers()

_VALIDATORS = {
    "assurance.execution.validator.closed-mapping.v1": ClosedMappingValidator(require_mapping=False),
    "assurance.execution.validator.evidence.v1": ExecutionEvidenceValidator(require_mapping=False),
}


class ExecutionPlugin(CapabilityPlugin):
    spec = CapabilitySpec(
        plugin_id="assurance.execution",
        version="0.2.0",
        engine_api=ENGINE_API_VERSION,
        source=EXECUTION_SOURCE,
        resource_bytes=resource_bytes,
        schema_files=_SCHEMA_FILES,
        resource_files=EXECUTION_RESOURCE_FILES,
        task_handlers=_HANDLERS,
        commit_validators=_VALIDATORS,
        dependencies=EXECUTION_DEPENDENCIES,
    )
