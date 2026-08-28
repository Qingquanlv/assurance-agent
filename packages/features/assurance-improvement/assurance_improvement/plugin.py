from __future__ import annotations

from graph_engine import ENGINE_API_VERSION
from graph_engine.plugin_api import (
    EffectPolicy,
    EffectRegistration,
    PluginContribution,
    PluginDependency,
    PluginDescriptor,
    ProviderSource,
    RegistryPorts,
    ResourceContribution,
    SchemaContribution,
)

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
from assurance_improvement.effects.store import InMemoryImprovementStore
from assurance_improvement.operations import improvement_handlers
from assurance_improvement.resource_loader import resource_bytes
from assurance_improvement.validators.archive import ArchiveIntegrityValidator
from assurance_improvement.validators.candidates import CandidatesValidator
from assurance_improvement.validators.delivery import DeliveryValidator
from assurance_improvement.validators.review import ReviewValidator

IMPROVEMENT_SOURCE = ProviderSource(
    distribution="assurance-improvement",
    version="0.1.0",
    entrypoint_group="graph_engine.plugins",
    entrypoint_name="improvement",
    entrypoint_value="assurance_improvement.plugin:ImprovementPlugin",
    declaration_path="assurance_improvement/plugin-declaration.json",
    import_roots=("",),
)

IMPROVEMENT_DEPENDENCIES: tuple[PluginDependency, ...] = (
    PluginDependency("assurance.intake", "==0.1.0"),
    PluginDependency("assurance.generation", "==0.1.0"),
    PluginDependency("assurance.execution", "==0.1.0"),
    PluginDependency("assurance.healing", "==0.1.0"),
    PluginDependency("assurance.quality", "==0.1.0"),
)

IMPROVEMENT_SCHEMA_IDS: tuple[str, ...] = (
    "assurance.improvement.schema.declaration-proposal.v1",
    "assurance.improvement.schema.improvement-candidates.v3",
    "assurance.improvement.schema.improvement-delivery.v1",
    "assurance.improvement.schema.improvement-effect-intent.v1",
    "assurance.improvement.schema.improvement-effect-receipt.v1",
    "assurance.improvement.schema.improvement-review.v1",
    "assurance.improvement.schema.promotion.v1",
    "assurance.improvement.schema.retro-context.v3",
    "assurance.improvement.schema.retro-signals.v3",
)

IMPROVEMENT_HANDLER_IDS: tuple[str, ...] = tuple(sorted(improvement_handlers()))

IMPROVEMENT_VALIDATOR_IDS: tuple[str, ...] = (
    "assurance.improvement.validator.archive-integrity.v1",
    "assurance.improvement.validator.candidates.v3",
    "assurance.improvement.validator.delivery.v1",
    "assurance.improvement.validator.review.v1",
)

IMPROVEMENT_EFFECT_IDS: tuple[str, ...] = (
    "assurance.improvement.effect.archive.v1",
    "assurance.improvement.effect.delivery.v1",
    "assurance.improvement.effect.promotion.v1",
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

IMPROVEMENT_RESOURCE_IDS: tuple[str, ...] = tuple(sorted(IMPROVEMENT_RESOURCE_FILES))

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
}

_VALIDATORS = {
    "assurance.improvement.validator.archive-integrity.v1": ArchiveIntegrityValidator(path_only=True),
    "assurance.improvement.validator.candidates.v3": CandidatesValidator(path_only=True),
    "assurance.improvement.validator.delivery.v1": DeliveryValidator(path_only=True),
    "assurance.improvement.validator.review.v1": ReviewValidator(path_only=True),
}


def _schema_contributions() -> tuple[SchemaContribution, ...]:
    return tuple(
        SchemaContribution(
            schema_id=schema_id,
            media_type="application/schema+json",
            content=resource_bytes(_SCHEMA_FILES[schema_id]),
        )
        for schema_id in IMPROVEMENT_SCHEMA_IDS
    )


def _resource_media_type(path: str) -> str:
    if path.endswith(".schema.json"):
        return "application/schema+json"
    return "text/plain"


def _resource_contributions() -> tuple[ResourceContribution, ...]:
    return tuple(
        ResourceContribution(
            resource_id=resource_id,
            media_type=_resource_media_type(IMPROVEMENT_RESOURCE_FILES[resource_id]),
            content=resource_bytes(IMPROVEMENT_RESOURCE_FILES[resource_id]),
        )
        for resource_id in IMPROVEMENT_RESOURCE_IDS
    )


def _effect_registrations() -> tuple[EffectRegistration, ...]:
    store = InMemoryImprovementStore()
    return (
        EffectRegistration(
            kind=ARCHIVE_KIND,
            intent_schema_id=ARCHIVE_INTENT_SCHEMA,
            receipt_schema_id=ARCHIVE_RECEIPT_SCHEMA,
            handler=ImprovementArchiveEffect(store=store),
            policy=ARCHIVE_POLICY,
        ),
        EffectRegistration(
            kind=DELIVERY_KIND,
            intent_schema_id=DELIVERY_INTENT_SCHEMA,
            receipt_schema_id=DELIVERY_RECEIPT_SCHEMA,
            handler=ImprovementDeliveryEffect(store=store),
            policy=DELIVERY_POLICY,
        ),
        EffectRegistration(
            kind=PROMOTION_KIND,
            intent_schema_id=PROMOTION_INTENT_SCHEMA,
            receipt_schema_id=PROMOTION_RECEIPT_SCHEMA,
            handler=ImprovementPromotionEffect(store=store),
            policy=PROMOTION_POLICY,
        ),
    )


class ImprovementPlugin:
    @staticmethod
    def descriptor() -> PluginDescriptor:
        return PluginDescriptor(
            schema_version="1",
            source=IMPROVEMENT_SOURCE,
            plugin_id="assurance.improvement",
            plugin_version="0.1.0",
            engine_api=ENGINE_API_VERSION,
            task_handlers=IMPROVEMENT_HANDLER_IDS,
            commit_validators=IMPROVEMENT_VALIDATOR_IDS,
            dependencies=IMPROVEMENT_DEPENDENCIES,
            schemas=IMPROVEMENT_SCHEMA_IDS,
            resources=IMPROVEMENT_RESOURCE_IDS,
            effects=IMPROVEMENT_EFFECT_IDS,
            bindings=(),
        )

    @staticmethod
    def contribute(ports: RegistryPorts) -> PluginContribution:
        if ports.engine_api != ENGINE_API_VERSION:
            raise ValueError(f"unsupported engine API: {ports.engine_api!r}")
        return PluginContribution(
            task_handlers=dict(improvement_handlers()),
            commit_validators=dict(sorted(_VALIDATORS.items())),
            schemas=_schema_contributions(),
            resources=_resource_contributions(),
            effects=_effect_registrations(),
        )
