from __future__ import annotations

import json

from graph_engine import ENGINE_API_VERSION
from graph_engine.canonical import canonical_json_bytes
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
from assurance_healing.effects.store import InMemoryHealingStore
from assurance_healing.operations import handlers as healing_task_handlers
from assurance_healing.resource_loader import resource_bytes
from assurance_healing.validators.override import OverrideValidator
from assurance_healing.validators.repair import RepairCandidateValidator
from assurance_healing.validators.test_tree import TestTreeValidator

HEALING_SOURCE = ProviderSource(
    distribution="assurance-healing",
    version="0.2.0",
    entrypoint_group="graph_engine.plugins",
    entrypoint_name="healing",
    entrypoint_value="assurance_healing.plugin:HealingPlugin",
    declaration_path="assurance_healing/plugin-declaration.json",
    import_roots=("",),
)

HEALING_SCHEMA_IDS: tuple[str, ...] = (
    "assurance.healing.schema.allocation-intent.v2",
    "assurance.healing.schema.allocation-receipt.v2",
    "assurance.healing.schema.coverage-repair.v1",
    "assurance.healing.schema.fix-proposal.v1",
    "assurance.healing.schema.heal-apply-intent.v2",
    "assurance.healing.schema.heal-apply-receipt.v2",
    "assurance.healing.schema.healing-safety.v1",
    "assurance.healing.schema.healing-status.v1",
    "assurance.healing.schema.proposal-approved-intent.v1",
    "assurance.healing.schema.proposal-approved-receipt.v1",
    "assurance.healing.workflow.repair-coverage.input.v1",
    "assurance.healing.workflow.repair-coverage.output.v1",
    "assurance.healing.workflow.repair-failure.input.v1",
    "assurance.healing.workflow.repair-failure.output.v1",
)

HEALING_DEPENDENCIES: tuple[PluginDependency, ...] = (
    PluginDependency("assurance.intake", "==0.2.0"),
    PluginDependency("assurance.generation", "==0.2.0"),
    PluginDependency("assurance.execution", "==0.2.0"),
)

HEALING_HANDLER_IDS: tuple[str, ...] = (
    "assurance.healing.allocate-coverage-repair-attempt",
    "assurance.healing.allocate-healing-attempt",
    "assurance.healing.combine-fixer-safety",
    "assurance.healing.compute-coverage-repair-safety",
    "assurance.healing.coverage-repair.finalize",
    "assurance.healing.coverage-repair.prepare",
    "assurance.healing.fix-proposal.finalize",
    "assurance.healing.fix-proposal.prepare",
    "assurance.healing.fixer-authority-ready",
    "assurance.healing.fixer-dispatch",
    "assurance.healing.project-episode",
    "assurance.healing.record-codegen-fix-apply",
    "assurance.healing.record-coverage-repair-status",
    "assurance.healing.record-fixer-approval",
    "assurance.healing.record-healing-status",
    "assurance.healing.repair-round.advance",
)

HEALING_VALIDATOR_IDS: tuple[str, ...] = (
    "assurance.healing.validator.override.v1",
    "assurance.healing.validator.repair-candidate.v1",
    "assurance.healing.validator.test-tree.v1",
)

HEALING_EFFECT_IDS: tuple[str, ...] = (
    "assurance.healing.effect.allocation.v2",
    "assurance.healing.effect.heal-apply.v2",
    "assurance.healing.effect.proposal-approved.v1",
)

ALLOCATION_POLICY = EffectPolicy(max_attempts=3, timeout_seconds=30.0, backoff_seconds=1.0)
APPROVAL_POLICY = EffectPolicy(max_attempts=3, timeout_seconds=30.0, backoff_seconds=1.0)
HEAL_APPLY_POLICY = EffectPolicy(max_attempts=5, timeout_seconds=120.0, backoff_seconds=2.0)

HEALING_RESOURCE_FILES: dict[str, str] = {
    "assurance.healing.persona.fix-proposer.v1": "personas/fix-proposer.md",
    "assurance.healing.policy.test-change-policy.v1": "policy/test-change-policy.v1.json",
    "assurance.healing.result.coverage-repair.v1": "result-contracts/coverage-repair.v1.schema.json",
    "assurance.healing.result.fix-proposal.v1": "result-contracts/fix-proposal.v1.schema.json",
    "assurance.healing.skill.aa-coverage-repair.v1": "skills/aa-coverage-repair/SKILL.md",
    "assurance.healing.skill.aa-fix-proposal.v1": "skills/aa-fix-proposal/SKILL.md",
    "assurance.healing.workflow.module.v1": "workflow/module.yaml",
}

HEALING_RESOURCE_IDS: tuple[str, ...] = tuple(sorted(HEALING_RESOURCE_FILES))

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
    "assurance.healing.workflow.repair-coverage.input.v1": (
        "schemas/workflow/repair-coverage-input.v1.schema.json"
    ),
    "assurance.healing.workflow.repair-coverage.output.v1": (
        "schemas/workflow/repair-coverage-output.v1.schema.json"
    ),
    "assurance.healing.workflow.repair-failure.input.v1": (
        "schemas/workflow/repair-failure-input.v1.schema.json"
    ),
    "assurance.healing.workflow.repair-failure.output.v1": (
        "schemas/workflow/repair-failure-output.v1.schema.json"
    ),
}

_VALIDATORS = {
    "assurance.healing.validator.override.v1": OverrideValidator(path_only=True),
    "assurance.healing.validator.repair-candidate.v1": RepairCandidateValidator(path_only=True),
    "assurance.healing.validator.test-tree.v1": TestTreeValidator(path_only=True),
}


def _schema_contributions() -> tuple[SchemaContribution, ...]:
    return tuple(
        SchemaContribution(
            schema_id=schema_id,
            media_type="application/schema+json",
            content=canonical_json_bytes(json.loads(resource_bytes(_SCHEMA_FILES[schema_id]))),
        )
        for schema_id in HEALING_SCHEMA_IDS
    )


_WORKFLOW_MODULE_MIME = "application/vnd.graph-engine.workflow-module+yaml"


def _resource_media_type(path: str) -> str:
    if path.endswith(".schema.json"):
        return "application/schema+json"
    if path.endswith("workflow/module.yaml"):
        return _WORKFLOW_MODULE_MIME
    if path.endswith(".json"):
        return "application/json"
    return "text/plain"


def _resource_contributions() -> tuple[ResourceContribution, ...]:
    return tuple(
        ResourceContribution(
            resource_id=resource_id,
            media_type=_resource_media_type(HEALING_RESOURCE_FILES[resource_id]),
            content=resource_bytes(HEALING_RESOURCE_FILES[resource_id]),
        )
        for resource_id in HEALING_RESOURCE_IDS
    )


def _effect_registrations() -> tuple[EffectRegistration, ...]:
    return (
        EffectRegistration(
            kind=ALLOCATION_KIND,
            intent_schema_id=ALLOCATION_INTENT_SCHEMA,
            receipt_schema_id=ALLOCATION_RECEIPT_SCHEMA,
            handler=HealingAllocationEffect(store=InMemoryHealingStore()),
            policy=ALLOCATION_POLICY,
        ),
        EffectRegistration(
            kind=HEAL_APPLY_KIND,
            intent_schema_id=HEAL_APPLY_INTENT_SCHEMA,
            receipt_schema_id=HEAL_APPLY_RECEIPT_SCHEMA,
            handler=HealApplyEffect(store=InMemoryHealingStore()),
            policy=HEAL_APPLY_POLICY,
        ),
        EffectRegistration(
            kind=APPROVAL_KIND,
            intent_schema_id=APPROVAL_INTENT_SCHEMA,
            receipt_schema_id=APPROVAL_RECEIPT_SCHEMA,
            handler=ProposalApprovedEffect(store=InMemoryHealingStore()),
            policy=APPROVAL_POLICY,
        ),
    )


class HealingPlugin:
    @staticmethod
    def descriptor() -> PluginDescriptor:
        return PluginDescriptor(
            schema_version="1",
            source=HEALING_SOURCE,
            plugin_id="assurance.healing",
            plugin_version="0.2.0",
            engine_api=ENGINE_API_VERSION,
            task_handlers=HEALING_HANDLER_IDS,
            commit_validators=HEALING_VALIDATOR_IDS,
            dependencies=HEALING_DEPENDENCIES,
            schemas=HEALING_SCHEMA_IDS,
            resources=HEALING_RESOURCE_IDS,
            effects=HEALING_EFFECT_IDS,
        )

    @staticmethod
    def contribute(ports: RegistryPorts) -> PluginContribution:
        if ports.engine_api != ENGINE_API_VERSION:
            raise ValueError(f"unsupported engine API: {ports.engine_api!r}")
        return PluginContribution(
            task_handlers=dict(healing_task_handlers()),
            commit_validators=_VALIDATORS,
            schemas=_schema_contributions(),
            resources=_resource_contributions(),
            effects=_effect_registrations(),
        )
