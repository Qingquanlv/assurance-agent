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

from assurance_generation.operations import generation_handlers
from assurance_generation.resource_loader import resource_bytes
from assurance_generation.validators import (
    CodegenFixCandidateValidator,
    CodegenMappingValidator,
    FamilyPlanValidator,
    GeneratedFilesValidator,
    PlanMechanicalValidator,
)

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
    "assurance.generation.workflow.generate.input.v1",
    "assurance.generation.workflow.generate.output.v1",
)

GENERATION_DEPENDENCIES: tuple[PluginDependency, ...] = (PluginDependency("assurance.intake", "==0.1.0"),)

GENERATION_HANDLER_IDS: tuple[str, ...] = (
    "assurance.generation.api.codegen-fix.finalize",
    "assurance.generation.api.codegen-fix.prepare",
    "assurance.generation.api.codegen.finalize",
    "assurance.generation.api.codegen.prepare",
    "assurance.generation.api.plan-review.finalize",
    "assurance.generation.api.plan-review.prepare",
    "assurance.generation.api.plan.finalize",
    "assurance.generation.api.plan.prepare",
    "assurance.generation.e2e.codegen-fix.finalize",
    "assurance.generation.e2e.codegen-fix.prepare",
    "assurance.generation.e2e.codegen.finalize",
    "assurance.generation.e2e.codegen.prepare",
    "assurance.generation.e2e.plan-review.finalize",
    "assurance.generation.e2e.plan-review.prepare",
    "assurance.generation.e2e.plan.finalize",
    "assurance.generation.e2e.plan.prepare",
    "assurance.generation.fuzz.codegen.finalize",
    "assurance.generation.fuzz.codegen.prepare",
    "assurance.generation.fuzz.plan-review.finalize",
    "assurance.generation.fuzz.plan-review.prepare",
    "assurance.generation.fuzz.plan.finalize",
    "assurance.generation.fuzz.plan.prepare",
    "assurance.generation.performance.codegen.finalize",
    "assurance.generation.performance.codegen.prepare",
    "assurance.generation.performance.plan-review.finalize",
    "assurance.generation.performance.plan-review.prepare",
    "assurance.generation.performance.plan.finalize",
    "assurance.generation.performance.plan.prepare",
)

GENERATION_VALIDATOR_IDS: tuple[str, ...] = (
    "assurance.generation.validator.api-plan.v1",
    "assurance.generation.validator.codegen-fix-candidate.v1",
    "assurance.generation.validator.codegen-mapping.v1",
    "assurance.generation.validator.e2e-plan.v1",
    "assurance.generation.validator.fuzz-plan.v1",
    "assurance.generation.validator.generated-files.v1",
    "assurance.generation.validator.performance-plan.v1",
    "assurance.generation.validator.plan-mechanical.v1",
)

GENERATION_RESOURCE_FILES: dict[str, str] = {
    "assurance.generation.persona.reviewer.v1": "personas/reviewer.md",
    "assurance.generation.persona.test-author.v1": "personas/test-author.md",
    "assurance.generation.result.codegen-fix.v1": "result-contracts/codegen-fix.v1.schema.json",
    "assurance.generation.result.codegen.v1": "result-contracts/codegen.v1.schema.json",
    "assurance.generation.result.plan-review.v1": "result-contracts/plan-review.v1.schema.json",
    "assurance.generation.result.plan.v1": "result-contracts/plan.v1.schema.json",
    "assurance.generation.skill.aa-api-codegen-fixer.v1": "skills/aa-api-codegen-fixer/SKILL.md",
    "assurance.generation.skill.aa-api-codegen.v1": "skills/aa-api-codegen/SKILL.md",
    "assurance.generation.skill.aa-api-plan-reviewer.v1": "skills/aa-api-plan-reviewer/SKILL.md",
    "assurance.generation.skill.aa-api-plan.v1": "skills/aa-api-plan/SKILL.md",
    "assurance.generation.skill.aa-e2e-codegen-fixer.v1": "skills/aa-e2e-codegen-fixer/SKILL.md",
    "assurance.generation.skill.aa-e2e-codegen.v1": "skills/aa-e2e-codegen/SKILL.md",
    "assurance.generation.skill.aa-e2e-plan-reviewer.v1": "skills/aa-e2e-plan-reviewer/SKILL.md",
    "assurance.generation.skill.aa-e2e-plan.v1": "skills/aa-e2e-plan/SKILL.md",
    "assurance.generation.skill.aa-fuzz-codegen.v1": "skills/aa-fuzz-codegen/SKILL.md",
    "assurance.generation.skill.aa-fuzz-plan-reviewer.v1": "skills/aa-fuzz-plan-reviewer/SKILL.md",
    "assurance.generation.skill.aa-fuzz-plan.v1": "skills/aa-fuzz-plan/SKILL.md",
    "assurance.generation.skill.aa-performance-codegen.v1": "skills/aa-performance-codegen/SKILL.md",
    "assurance.generation.skill.aa-performance-plan-reviewer.v1": "skills/aa-performance-plan-reviewer/SKILL.md",
    "assurance.generation.skill.aa-performance-plan.v1": "skills/aa-performance-plan/SKILL.md",
    "assurance.generation.workflow.module.v1": "workflow/module.yaml",
}

GENERATION_RESOURCE_IDS: tuple[str, ...] = tuple(sorted(GENERATION_RESOURCE_FILES))

_SCHEMA_FILES: dict[str, str] = {
    "assurance.generation.schema.codegen-mapping.v1": "schemas/codegen-mapping.v1.schema.json",
    "assurance.generation.schema.discovery-campaign.v1": "schemas/discovery-campaign.v1.schema.json",
    "assurance.generation.schema.generated-files.v1": "schemas/generated-files.v1.schema.json",
    "assurance.generation.schema.plan-check.v1": "schemas/plan-check.v1.schema.json",
    "assurance.generation.schema.plan-review.v1": "schemas/plan-review.v1.schema.json",
    "assurance.generation.workflow.generate.input.v1": "schemas/workflow/generate-input.v1.schema.json",
    "assurance.generation.workflow.generate.output.v1": "schemas/workflow/generate-output.v1.schema.json",
}

_VALIDATORS = {
    "assurance.generation.validator.api-plan.v1": FamilyPlanValidator("api"),
    "assurance.generation.validator.e2e-plan.v1": FamilyPlanValidator("e2e"),
    "assurance.generation.validator.fuzz-plan.v1": FamilyPlanValidator("fuzz"),
    "assurance.generation.validator.performance-plan.v1": FamilyPlanValidator("performance"),
    "assurance.generation.validator.plan-mechanical.v1": PlanMechanicalValidator(),
    "assurance.generation.validator.generated-files.v1": GeneratedFilesValidator(require_mapping=False),
    "assurance.generation.validator.codegen-mapping.v1": CodegenMappingValidator(),
    "assurance.generation.validator.codegen-fix-candidate.v1": CodegenFixCandidateValidator(),
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
            media_type=_resource_media_type(GENERATION_RESOURCE_FILES[resource_id]),
            content=resource_bytes(GENERATION_RESOURCE_FILES[resource_id]),
        )
        for resource_id in GENERATION_RESOURCE_IDS
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
            task_handlers=GENERATION_HANDLER_IDS,
            commit_validators=GENERATION_VALIDATOR_IDS,
            dependencies=GENERATION_DEPENDENCIES,
            schemas=GENERATION_SCHEMA_IDS,
            resources=GENERATION_RESOURCE_IDS,
        )

    @staticmethod
    def contribute(ports: RegistryPorts) -> PluginContribution:
        if ports.engine_api != ENGINE_API_VERSION:
            raise ValueError(f"unsupported engine API: {ports.engine_api!r}")
        return PluginContribution(
            task_handlers=generation_handlers(),
            commit_validators=_VALIDATORS,
            schemas=_schema_contributions(),
            resources=_resource_contributions(),
        )
