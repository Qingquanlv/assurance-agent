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

from assurance_healing.contracts.attempts import attempt_contract_refs
from assurance_healing.ops import router

from assurance_healing.operations import handlers as healing_task_handlers
from assurance_healing.resource_loader import resource_bytes

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


HEALING_RESOURCE_FILES: dict[str, str] = dict(router.resource_files())

_SCHEMA_FILES: dict[str, str] = {
    "assurance.healing.schema.fix-proposal.v1": "schemas/fix-proposal.v1.schema.json",
    "assurance.healing.schema.healing-safety.v1": "schemas/healing-safety.v1.schema.json",
    "assurance.healing.schema.healing-status.v1": "schemas/healing-status.v1.schema.json",
}

_GENERATED_SCHEMAS: dict[str, str] = {
    "assurance.healing.schema.fix-proposal.v1": "assurance_healing.contracts:FixProposal",
    "assurance.healing.schema.healing-safety.v1": "assurance_healing.contracts:SafetyCheck",
    "assurance.healing.schema.healing-status.v1": "assurance_healing.contracts:HealingStatusV1",
}

_VALIDATORS: dict[str, CommitValidator] = {}


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
    )

    @classmethod
    def descriptor(cls) -> PluginDescriptor:
        return cls.spec.descriptor().model_copy(update={"attempt_contracts": attempt_contract_refs()})

    @classmethod
    def contribute(cls, ports: RegistryPorts) -> PluginContribution:
        return replace(cls.spec.contribution(ports), attempt_contracts=attempt_contract_refs())
