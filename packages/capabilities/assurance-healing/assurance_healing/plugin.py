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

from assurance_healing.contracts.attempts import attempt_contract_refs
from assurance_healing.ops import router

from assurance_healing.effects.allocation import (
    ALLOCATION_INTENT_SCHEMA,
    ALLOCATION_KIND,
    ALLOCATION_RECEIPT_SCHEMA,
    HealingAllocationEffect,
)
from assurance_healing.effects.apply import (
    HEAL_APPLY_INTENT_SCHEMA,
    HEAL_APPLY_KIND,
    HEAL_APPLY_RECEIPT_SCHEMA,
    HealApplyEffect,
)
from assurance_healing.effects.approval import (
    APPROVAL_INTENT_SCHEMA,
    APPROVAL_KIND,
    APPROVAL_RECEIPT_SCHEMA,
    ProposalApprovedEffect,
)
from assurance_healing.operations import handlers as healing_task_handlers
from assurance_healing.resource_loader import resource_bytes
from assurance_healing.validators.override import OverrideValidator
from assurance_healing.validators.repair import RepairCandidateValidator
from assurance_healing.validators.test_tree import TestTreeValidator

HEALING_SOURCE = ProviderSource(
    distribution="assurance-healing",
    version="0.3.0",
    entrypoint_group="graph_engine.plugins",
    entrypoint_name="healing",
    entrypoint_value="assurance_healing.plugin:HealingPlugin",
    declaration_path="assurance_healing/plugin-declaration.json",
    import_roots=("",),
)

HEALING_DEPENDENCIES: tuple[PluginDependency, ...] = (
    PluginDependency("assurance.intake", "==0.3.0"),
    PluginDependency("assurance.generation", "==0.3.0"),
    PluginDependency("assurance.execution", "==0.3.0"),
)

ALLOCATION_POLICY = EffectPolicy(max_attempts=3, timeout_seconds=30.0, backoff_seconds=1.0)
APPROVAL_POLICY = EffectPolicy(max_attempts=3, timeout_seconds=30.0, backoff_seconds=1.0)
HEAL_APPLY_POLICY = EffectPolicy(max_attempts=5, timeout_seconds=120.0, backoff_seconds=2.0)

HEALING_RESOURCE_FILES: dict[str, str] = {
    **router.resource_files(),
    "assurance.healing.policy.test-change-policy.v1": "policy/test-change-policy.v1.json",
}

_SCHEMA_FILES: dict[str, str] = {
    "assurance.healing.schema.allocation-intent.v2": "schemas/allocation-intent.v2.schema.json",
    "assurance.healing.schema.allocation-receipt.v2": "schemas/allocation-receipt.v2.schema.json",
    "assurance.healing.schema.coverage-repair.v1": "schemas/coverage-repair.v1.schema.json",
    "assurance.healing.schema.fix-proposal.v1": "schemas/fix-proposal.v1.schema.json",
    "assurance.healing.schema.heal-apply-intent.v2": "schemas/heal-apply-intent.v2.schema.json",
    "assurance.healing.schema.heal-apply-receipt.v2": "schemas/heal-apply-receipt.v2.schema.json",
    "assurance.healing.schema.healing-safety.v1": "schemas/healing-safety.v1.schema.json",
    "assurance.healing.schema.healing-status.v1": "schemas/healing-status.v1.schema.json",
    "assurance.healing.schema.proposal-approved-intent.v1": (
        "schemas/proposal-approved-intent.v1.schema.json"
    ),
    "assurance.healing.schema.proposal-approved-receipt.v1": (
        "schemas/proposal-approved-receipt.v1.schema.json"
    ),
}

_GENERATED_SCHEMAS: dict[str, str] = {
    "assurance.healing.schema.allocation-intent.v2": "assurance_healing.contracts:HealingAllocationIntentV2",
    "assurance.healing.schema.allocation-receipt.v2": (
        "assurance_healing.contracts:HealingAllocationReceiptV2"
    ),
    "assurance.healing.schema.coverage-repair.v1": "assurance_healing.contracts:CoverageRepairBrief",
    "assurance.healing.schema.fix-proposal.v1": "assurance_healing.contracts:FixProposal",
    "assurance.healing.schema.heal-apply-intent.v2": "assurance_healing.contracts:HealApplyIntentV2",
    "assurance.healing.schema.heal-apply-receipt.v2": "assurance_healing.contracts:HealApplyReceiptV2",
    "assurance.healing.schema.healing-safety.v1": "assurance_healing.contracts:SafetyCheck",
    "assurance.healing.schema.healing-status.v1": "assurance_healing.contracts:HealingStatusV1",
    "assurance.healing.schema.proposal-approved-intent.v1": (
        "assurance_healing.contracts:ProposalApprovedIntentV1"
    ),
    "assurance.healing.schema.proposal-approved-receipt.v1": (
        "assurance_healing.contracts:ProposalApprovedReceiptV1"
    ),
}

_VALIDATORS = {
    "assurance.healing.validator.override.v1": OverrideValidator(path_only=True),
    "assurance.healing.validator.repair-candidate.v1": RepairCandidateValidator(path_only=True),
    "assurance.healing.validator.test-tree.v1": TestTreeValidator(path_only=True),
}


def _effect_registrations() -> tuple[EffectRegistration, ...]:
    return (
        EffectRegistration(
            kind=ALLOCATION_KIND,
            intent_schema_id=ALLOCATION_INTENT_SCHEMA,
            receipt_schema_id=ALLOCATION_RECEIPT_SCHEMA,
            handler=HealingAllocationEffect(),
            policy=ALLOCATION_POLICY,
        ),
        EffectRegistration(
            kind=HEAL_APPLY_KIND,
            intent_schema_id=HEAL_APPLY_INTENT_SCHEMA,
            receipt_schema_id=HEAL_APPLY_RECEIPT_SCHEMA,
            handler=HealApplyEffect(),
            policy=HEAL_APPLY_POLICY,
        ),
        EffectRegistration(
            kind=APPROVAL_KIND,
            intent_schema_id=APPROVAL_INTENT_SCHEMA,
            receipt_schema_id=APPROVAL_RECEIPT_SCHEMA,
            handler=ProposalApprovedEffect(),
            policy=APPROVAL_POLICY,
        ),
    )


_HANDLERS = healing_task_handlers()


class HealingPlugin(CapabilityPlugin):
    generated_schemas = _GENERATED_SCHEMAS
    spec = CapabilitySpec(
        plugin_id="assurance.healing",
        version="0.3.0",
        engine_api=ENGINE_API_VERSION,
        source=HEALING_SOURCE,
        resource_bytes=resource_bytes,
        schema_files=_SCHEMA_FILES,
        resource_files=HEALING_RESOURCE_FILES,
        task_handlers=_HANDLERS,
        commit_validators=_VALIDATORS,
        dependencies=HEALING_DEPENDENCIES,
        effects=_effect_registrations,
    )

    @classmethod
    def descriptor(cls) -> PluginDescriptor:
        return cls.spec.descriptor().model_copy(update={"attempt_contracts": attempt_contract_refs()})

    @classmethod
    def contribute(cls, ports: RegistryPorts) -> PluginContribution:
        return replace(cls.spec.contribution(ports), attempt_contracts=attempt_contract_refs())
