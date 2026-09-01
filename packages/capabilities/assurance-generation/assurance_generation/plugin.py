from __future__ import annotations

from dataclasses import replace

from graph_engine import ENGINE_API_VERSION
from graph_engine.plugin_api import (
    PluginContribution,
    PluginDependency,
    PluginDescriptor,
    ProviderSource,
    RegistryPorts,
)
from graph_engine.plugin_kit import CapabilityPlugin, CapabilitySpec

from assurance_generation.contracts.attempts import attempt_contract_refs
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
    version="0.2.0",
    entrypoint_group="graph_engine.plugins",
    entrypoint_name="generation",
    entrypoint_value="assurance_generation.plugin:GenerationPlugin",
    declaration_path="assurance_generation/plugin-declaration.json",
    import_roots=("",),
)

GENERATION_DEPENDENCIES: tuple[PluginDependency, ...] = (PluginDependency("assurance.intake", "==0.2.0"),)

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

_SCHEMA_FILES: dict[str, str] = {
    "assurance.generation.schema.codegen-mapping.v1": "schemas/codegen-mapping.v1.schema.json",
    "assurance.generation.schema.discovery-campaign.v1": "schemas/discovery-campaign.v1.schema.json",
    "assurance.generation.schema.generated-files.v1": "schemas/generated-files.v1.schema.json",
    "assurance.generation.schema.plan-check.v1": "schemas/plan-check.v1.schema.json",
    "assurance.generation.schema.plan-review.v1": "schemas/plan-review.v1.schema.json",
    "assurance.generation.workflow.generate.input.v1": "schemas/workflow/generate-input.v1.schema.json",
    "assurance.generation.workflow.generate.output.v1": "schemas/workflow/generate-output.v1.schema.json",
}

_HANDLERS = generation_handlers()

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


class GenerationPlugin(CapabilityPlugin):
    spec = CapabilitySpec(
        plugin_id="assurance.generation",
        version="0.2.0",
        engine_api=ENGINE_API_VERSION,
        source=GENERATION_SOURCE,
        resource_bytes=resource_bytes,
        schema_files=_SCHEMA_FILES,
        resource_files=GENERATION_RESOURCE_FILES,
        task_handlers=_HANDLERS,
        commit_validators=_VALIDATORS,
        dependencies=GENERATION_DEPENDENCIES,
    )

    @classmethod
    def descriptor(cls) -> PluginDescriptor:
        return cls.spec.descriptor().model_copy(update={"attempt_contracts": attempt_contract_refs()})

    @classmethod
    def contribute(cls, ports: RegistryPorts) -> PluginContribution:
        return replace(cls.spec.contribution(ports), attempt_contracts=attempt_contract_refs())
