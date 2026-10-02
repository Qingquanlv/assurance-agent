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
from assurance_generation.ops import router
from assurance_generation.resource_loader import resource_bytes
from assurance_generation.validators import (
    CodegenMappingValidator,
    GeneratedFilesValidator,
)

GENERATION_SOURCE = ProviderSource(
    distribution="assurance-generation",
    version="0.3.0",
    entrypoint_group="graph_engine.plugins",
    entrypoint_name="generation",
    entrypoint_value="assurance_generation.plugin:GenerationPlugin",
    declaration_path="assurance_generation/plugin-declaration.json",
    import_roots=("",),
)

GENERATION_DEPENDENCIES: tuple[PluginDependency, ...] = (PluginDependency("assurance.intake", "==0.3.0"),)

GENERATION_RESOURCE_FILES: dict[str, str] = {
    **router.resource_files(),
    "assurance.generation.result.plan.v1": "result-contracts/plan.v1.schema.json",
}

_SCHEMA_FILES: dict[str, str] = {
    "assurance.generation.schema.codegen-mapping.v1": "schemas/codegen-mapping.v1.schema.json",
    "assurance.generation.schema.discovery-campaign.v1": "schemas/discovery-campaign.v1.schema.json",
    "assurance.generation.schema.generated-files.v1": "schemas/generated-files.v1.schema.json",
    "assurance.generation.schema.plan-check.v1": "schemas/plan-check.v1.schema.json",
    "assurance.generation.schema.plan-review.v1": "schemas/plan-review.v1.schema.json",
}

_GENERATED_SCHEMAS: dict[str, str] = {
    "assurance.generation.schema.codegen-mapping.v1": "assurance_generation.contracts:CodegenMapping",
    "assurance.generation.schema.discovery-campaign.v1": "assurance_generation.contracts:CampaignSpec",
    "assurance.generation.schema.generated-files.v1": "assurance_generation.contracts:GeneratedFilesV1",
    "assurance.generation.schema.plan-check.v1": "assurance_generation.contracts:PlanCheckDocument",
    "assurance.generation.schema.plan-review.v1": "assurance_generation.contracts:PlanReviewAuthoring",
}

_HANDLERS = generation_handlers()

_VALIDATORS = {
    "assurance.generation.validator.generated-files.v1": GeneratedFilesValidator(require_mapping=False),
    "assurance.generation.validator.codegen-mapping.v1": CodegenMappingValidator(),
}


class GenerationPlugin(CapabilityPlugin):
    generated_schemas = _GENERATED_SCHEMAS
    spec = CapabilitySpec(
        plugin_id="assurance.generation",
        version="0.3.0",
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
