from __future__ import annotations

from dataclasses import replace
from typing import cast

from graph_engine import ENGINE_API_VERSION
from graph_engine.plugin_api import (
    PluginContribution,
    PluginDescriptor,
    ProviderSource,
    RegistryPorts,
    TaskHandler,
)
from graph_engine.plugin_kit import CapabilityPlugin, CapabilitySpec

from assurance_intake import ops
from assurance_intake.validators import CaseCandidateValidator, CaseReferenceValidator

INTAKE_SOURCE = ProviderSource(
    distribution="assurance-intake",
    version="0.3.0",
    entrypoint_group="graph_engine.plugins",
    entrypoint_name="intake",
    entrypoint_value="assurance_intake.plugin:IntakePlugin",
    declaration_path="assurance_intake/plugin-declaration.json",
    import_roots=("",),
)

_SCHEMA_FILES: dict[str, str] = {
    "assurance.intake.schema.resolved-assurance-plan.v1": (
        "contracts/schemas/resolved-assurance-plan.v1.schema.json"
    ),
    "assurance.intake.schema.case-authoring.v1": "contracts/schemas/case-authoring.v1.schema.json",
    "assurance.intake.schema.case-review.v1": "contracts/schemas/case-review.v1.schema.json",
    "assurance.intake.schema.case.v1": "contracts/schemas/case.v1.schema.json",
    "assurance.intake.schema.case-selection.v1": "contracts/schemas/case-selection.v1.schema.json",
    "assurance.intake.schema.qa-change.v1": "contracts/schemas/qa-change.v1.schema.json",
    "assurance.intake.workflow.case.input.v1": "contracts/schemas/workflow/case-input.v1.schema.json",
    "assurance.intake.workflow.case.output.v1": "contracts/schemas/workflow/case-output.v1.schema.json",
    "assurance.intake.workflow.prepare.input.v1": "contracts/schemas/workflow/prepare-input.v1.schema.json",
    "assurance.intake.workflow.prepare.output.v1": (
        "contracts/schemas/workflow/prepare-output.v1.schema.json"
    ),
}

_VALIDATORS = {
    "assurance.intake.validator.case-candidate.v1": CaseCandidateValidator(),
    "assurance.intake.validator.case-references.v1": CaseReferenceValidator(),
}


class IntakePlugin(CapabilityPlugin):
    spec = CapabilitySpec(
        plugin_id="assurance.intake",
        version="0.3.0",
        engine_api=ENGINE_API_VERSION,
        source=INTAKE_SOURCE,
        resource_bytes=ops.router.resource_bytes,
        schema_files=_SCHEMA_FILES,
        resource_files=dict(ops.router.resource_files()),
        task_handlers=dict(ops.router.handlers(cast(TaskHandler, ops))),
        commit_validators=_VALIDATORS,
    )

    @classmethod
    def descriptor(cls) -> PluginDescriptor:
        return cls.spec.descriptor().model_copy(
            update={"attempt_contracts": ops.router.attempt_contract_refs()}
        )

    @classmethod
    def contribute(cls, ports: RegistryPorts) -> PluginContribution:
        return replace(cls.spec.contribution(ports), attempt_contracts=ops.router.attempt_contract_refs())
