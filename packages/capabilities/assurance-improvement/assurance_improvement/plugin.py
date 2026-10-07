from __future__ import annotations

from dataclasses import replace

from graph_engine import ENGINE_API_VERSION
from graph_engine.plugin_api import (
    CommitValidator,
    PluginContribution,
    PluginDependency,
    PluginDescriptor,
    ProviderSource,
    RegistryPorts,
)
from graph_engine.plugin_kit import CapabilityPlugin, CapabilitySpec

from assurance_improvement.contracts.attempts import attempt_contract_refs
from assurance_improvement.ops import router

from assurance_improvement.operations import improvement_handlers
from assurance_improvement.resource_loader import resource_bytes

IMPROVEMENT_SOURCE = ProviderSource(
    distribution="assurance-improvement",
    version="0.3.0",
    entrypoint_group="graph_engine.plugins",
    entrypoint_name="improvement",
    entrypoint_value="assurance_improvement.plugin:ImprovementPlugin",
    declaration_path="assurance_improvement/plugin-declaration.json",
    import_roots=("",),
)

IMPROVEMENT_DEPENDENCIES: tuple[PluginDependency, ...] = (
    PluginDependency("assurance.intake", "==0.3.0"),
    PluginDependency("assurance.generation", "==0.3.0"),
    PluginDependency("assurance.execution", "==0.3.0"),
    PluginDependency("assurance.healing", "==0.3.0"),
    PluginDependency("assurance.quality", "==0.3.0"),
)


IMPROVEMENT_RESOURCE_FILES: dict[str, str] = dict(router.resource_files())

_SCHEMA_FILES: dict[str, str] = {
    "assurance.improvement.schema.declaration-proposal.v1": "schemas/declaration-proposal.v1.schema.json",
    "assurance.improvement.schema.improvement-candidates.v3": (
        "schemas/improvement-candidates.v3.schema.json"
    ),
    "assurance.improvement.schema.improvement-delivery.v1": "schemas/improvement-delivery.v1.schema.json",
    "assurance.improvement.schema.improvement-review.v1": "schemas/improvement-review.v1.schema.json",
    "assurance.improvement.schema.promotion.v1": "schemas/promotion.v1.schema.json",
    "assurance.improvement.schema.retro-context.v3": "schemas/retro-context.v3.schema.json",
    "assurance.improvement.schema.retro-signals.v3": "schemas/retro-signals.v3.schema.json",
}

_GENERATED_SCHEMAS: dict[str, str] = {
    "assurance.improvement.schema.declaration-proposal.v1": (
        "assurance_improvement.contracts:DeclarationProposal"
    ),
    "assurance.improvement.schema.improvement-candidates.v3": (
        "assurance_improvement.contracts:ImprovementCandidateDocumentV3"
    ),
    "assurance.improvement.schema.improvement-delivery.v1": (
        "assurance_improvement.contracts:ImprovementDeliveryDocument"
    ),
    "assurance.improvement.schema.improvement-review.v1": (
        "assurance_improvement.contracts:ImprovementReviewSubject"
    ),
    "assurance.improvement.schema.promotion.v1": "assurance_improvement.contracts:TestPromotionManifest",
    "assurance.improvement.schema.retro-context.v3": "assurance_improvement.contracts:RetroContextV3",
    "assurance.improvement.schema.retro-signals.v3": "assurance_improvement.contracts:SignalDocumentV3",
}

_VALIDATORS: dict[str, CommitValidator] = {}


_HANDLERS = improvement_handlers()


class ImprovementPlugin(CapabilityPlugin):
    generated_schemas = _GENERATED_SCHEMAS
    spec = CapabilitySpec(
        plugin_id="assurance.improvement",
        version="0.3.0",
        engine_api=ENGINE_API_VERSION,
        source=IMPROVEMENT_SOURCE,
        resource_bytes=resource_bytes,
        schema_files=_SCHEMA_FILES,
        resource_files=IMPROVEMENT_RESOURCE_FILES,
        task_handlers=_HANDLERS,
        commit_validators=_VALIDATORS,
        dependencies=IMPROVEMENT_DEPENDENCIES,
    )

    @classmethod
    def descriptor(cls) -> PluginDescriptor:
        return cls.spec.descriptor().model_copy(update={"attempt_contracts": attempt_contract_refs()})

    @classmethod
    def contribute(cls, ports: RegistryPorts) -> PluginContribution:
        return replace(cls.spec.contribution(ports), attempt_contracts=attempt_contract_refs())
