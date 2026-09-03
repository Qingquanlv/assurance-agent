from __future__ import annotations

from dataclasses import replace

from graph_engine import ENGINE_API_VERSION
from graph_engine.plugin_api import (
    EffectPolicy,
    EffectRegistration,
    PluginContribution,
    PluginDependency,
    PluginDescriptor,
    ProviderSource,
    RegistryPorts,
)
from graph_engine.plugin_kit import CapabilityPlugin, CapabilitySpec

from assurance_improvement.contracts.attempts import attempt_contract_refs

from assurance_improvement.effects.archive import (
    ARCHIVE_INTENT_SCHEMA,
    ARCHIVE_KIND,
    ARCHIVE_RECEIPT_SCHEMA,
    ImprovementArchiveEffect,
)
from assurance_improvement.effects.delivery import (
    DELIVERY_INTENT_SCHEMA,
    DELIVERY_KIND,
    DELIVERY_RECEIPT_SCHEMA,
    ImprovementDeliveryEffect,
)
from assurance_improvement.effects.promotion import (
    PROMOTION_INTENT_SCHEMA,
    PROMOTION_KIND,
    PROMOTION_RECEIPT_SCHEMA,
    ImprovementPromotionEffect,
)
from assurance_improvement.operations import improvement_handlers
from assurance_improvement.resource_loader import resource_bytes
from assurance_improvement.validators.archive import ArchiveIntegrityValidator
from assurance_improvement.validators.candidates import CandidatesValidator
from assurance_improvement.validators.delivery import DeliveryValidator
from assurance_improvement.validators.review import ReviewValidator

IMPROVEMENT_SOURCE = ProviderSource(
    distribution="assurance-improvement",
    version="0.2.0",
    entrypoint_group="graph_engine.plugins",
    entrypoint_name="improvement",
    entrypoint_value="assurance_improvement.plugin:ImprovementPlugin",
    declaration_path="assurance_improvement/plugin-declaration.json",
    import_roots=("",),
)

IMPROVEMENT_DEPENDENCIES: tuple[PluginDependency, ...] = (
    PluginDependency("assurance.intake", "==0.2.0"),
    PluginDependency("assurance.generation", "==0.2.0"),
    PluginDependency("assurance.execution", "==0.2.0"),
    PluginDependency("assurance.healing", "==0.2.0"),
    PluginDependency("assurance.quality", "==0.2.0"),
)

DELIVERY_POLICY = EffectPolicy(max_attempts=5, timeout_seconds=120.0, backoff_seconds=2.0)
PROMOTION_POLICY = EffectPolicy(max_attempts=3, timeout_seconds=60.0, backoff_seconds=2.0)
ARCHIVE_POLICY = EffectPolicy(max_attempts=3, timeout_seconds=60.0, backoff_seconds=2.0)

IMPROVEMENT_RESOURCE_FILES: dict[str, str] = {
    "assurance.improvement.persona.archiver.v1": "personas/archiver.md",
    "assurance.improvement.persona.reviewer.v1": "personas/reviewer.md",
    "assurance.improvement.prompt.archive-summary.v1": "skills/aa-archive/archive-summary-template.md",
    "assurance.improvement.result.archive.v1": "result-contracts/archive.v1.schema.json",
    "assurance.improvement.result.improvement-review.v1": (
        "result-contracts/improvement-review.v1.schema.json"
    ),
    "assurance.improvement.result.retro-analysis.v3": "result-contracts/retro-analysis.v3.schema.json",
    "assurance.improvement.skill.aa-archive.v1": "skills/aa-archive/SKILL.md",
    "assurance.improvement.skill.aa-improvement-reviewer.v1": "skills/aa-improvement-reviewer/SKILL.md",
    "assurance.improvement.skill.aa-retro-eval-analysis.v1": "skills/aa-retro-eval-analysis/SKILL.md",
    "assurance.improvement.skill.aa-retro-issue-analysis.v1": "skills/aa-retro-issue-analysis/SKILL.md",
    "assurance.improvement.skill.aa-retro-workflow-analysis.v1": (
        "skills/aa-retro-workflow-analysis/SKILL.md"
    ),
    "assurance.improvement.skill.aa-retro.v1": "skills/aa-retro/SKILL.md",
}

_SCHEMA_FILES: dict[str, str] = {
    "assurance.improvement.schema.declaration-proposal.v1": "schemas/declaration-proposal.v1.schema.json",
    "assurance.improvement.schema.improvement-candidates.v3": (
        "schemas/improvement-candidates.v3.schema.json"
    ),
    "assurance.improvement.schema.improvement-delivery.v1": "schemas/improvement-delivery.v1.schema.json",
    "assurance.improvement.schema.improvement-effect-intent.v1": (
        "schemas/improvement-effect-intent.v1.schema.json"
    ),
    "assurance.improvement.schema.improvement-effect-receipt.v1": (
        "schemas/improvement-effect-receipt.v1.schema.json"
    ),
    "assurance.improvement.schema.improvement-review.v1": "schemas/improvement-review.v1.schema.json",
    "assurance.improvement.schema.promotion.v1": "schemas/promotion.v1.schema.json",
    "assurance.improvement.schema.retro-context.v3": "schemas/retro-context.v3.schema.json",
    "assurance.improvement.schema.retro-signals.v3": "schemas/retro-signals.v3.schema.json",
    "assurance.improvement.workflow.apply.input.v1": "schemas/workflow/apply-input.v1.schema.json",
    "assurance.improvement.workflow.apply.output.v1": "schemas/workflow/apply-output.v1.schema.json",
    "assurance.improvement.workflow.archive.input.v1": "schemas/workflow/archive-input.v1.schema.json",
    "assurance.improvement.workflow.archive.output.v1": "schemas/workflow/archive-output.v1.schema.json",
    "assurance.improvement.workflow.evaluate.input.v1": "schemas/workflow/evaluate-input.v1.schema.json",
    "assurance.improvement.workflow.evaluate.output.v1": ("schemas/workflow/evaluate-output.v1.schema.json"),
    "assurance.improvement.workflow.export.input.v1": "schemas/workflow/export-input.v1.schema.json",
    "assurance.improvement.workflow.export.output.v1": "schemas/workflow/export-output.v1.schema.json",
    "assurance.improvement.workflow.retro.input.v1": "schemas/workflow/retro-input.v1.schema.json",
    "assurance.improvement.workflow.retro.output.v1": "schemas/workflow/retro-output.v1.schema.json",
    "assurance.improvement.workflow.review.input.v1": "schemas/workflow/review-input.v1.schema.json",
    "assurance.improvement.workflow.review.output.v1": "schemas/workflow/review-output.v1.schema.json",
    "assurance.improvement.workflow.rollback.input.v1": "schemas/workflow/rollback-input.v1.schema.json",
    "assurance.improvement.workflow.rollback.output.v1": ("schemas/workflow/rollback-output.v1.schema.json"),
}

_VALIDATORS = {
    "assurance.improvement.validator.archive-integrity.v1": ArchiveIntegrityValidator(path_only=True),
    "assurance.improvement.validator.candidates.v3": CandidatesValidator(path_only=True),
    "assurance.improvement.validator.delivery.v1": DeliveryValidator(path_only=True),
    "assurance.improvement.validator.review.v1": ReviewValidator(path_only=True),
}


def _effect_registrations() -> tuple[EffectRegistration, ...]:
    return (
        EffectRegistration(
            kind=ARCHIVE_KIND,
            intent_schema_id=ARCHIVE_INTENT_SCHEMA,
            receipt_schema_id=ARCHIVE_RECEIPT_SCHEMA,
            handler=ImprovementArchiveEffect(),
            policy=ARCHIVE_POLICY,
        ),
        EffectRegistration(
            kind=DELIVERY_KIND,
            intent_schema_id=DELIVERY_INTENT_SCHEMA,
            receipt_schema_id=DELIVERY_RECEIPT_SCHEMA,
            handler=ImprovementDeliveryEffect(),
            policy=DELIVERY_POLICY,
        ),
        EffectRegistration(
            kind=PROMOTION_KIND,
            intent_schema_id=PROMOTION_INTENT_SCHEMA,
            receipt_schema_id=PROMOTION_RECEIPT_SCHEMA,
            handler=ImprovementPromotionEffect(),
            policy=PROMOTION_POLICY,
        ),
    )


_HANDLERS = improvement_handlers()


class ImprovementPlugin(CapabilityPlugin):
    spec = CapabilitySpec(
        plugin_id="assurance.improvement",
        version="0.2.0",
        engine_api=ENGINE_API_VERSION,
        source=IMPROVEMENT_SOURCE,
        resource_bytes=resource_bytes,
        schema_files=_SCHEMA_FILES,
        resource_files=IMPROVEMENT_RESOURCE_FILES,
        task_handlers=_HANDLERS,
        commit_validators=_VALIDATORS,
        dependencies=IMPROVEMENT_DEPENDENCIES,
        effects=_effect_registrations,
    )

    @classmethod
    def descriptor(cls) -> PluginDescriptor:
        return cls.spec.descriptor().model_copy(update={"attempt_contracts": attempt_contract_refs()})

    @classmethod
    def contribute(cls, ports: RegistryPorts) -> PluginContribution:
        return replace(cls.spec.contribution(ports), attempt_contracts=attempt_contract_refs())
