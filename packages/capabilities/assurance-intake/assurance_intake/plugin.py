from __future__ import annotations

from dataclasses import replace

from graph_engine import ENGINE_API_VERSION
from graph_engine.plugin_api import PluginContribution, PluginDescriptor, ProviderSource, RegistryPorts
from graph_engine.plugin_kit import CapabilityPlugin, CapabilitySpec

from assurance_intake.contracts.attempts import attempt_contract_refs
from assurance_intake.operations import (
    CaseDesignFinalizeHandler,
    CaseDesignPrepareHandler,
    CaseReviewFinalizeHandler,
    CaseReviewPrepareHandler,
    ExploreFinalizeHandler,
    ExplorePrepareHandler,
    IntakeFinalizeHandler,
    IntakePrepareHandler,
    ResolvePlanHandler,
    ReviewRoundAdvanceHandler,
)
from assurance_intake.resource_loader import resource_bytes
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

INTAKE_RESOURCE_FILES: dict[str, str] = {
    "assurance.intake.persona.doc-author.v1": "personas/doc-author.md",
    "assurance.intake.persona.explorer.v1": "personas/explorer.md",
    "assurance.intake.persona.intake-host.v1": "personas/intake-host.md",
    "assurance.intake.persona.reviewer.v1": "personas/reviewer.md",
    "assurance.intake.result.case-design.v1": "result-contracts/case-design.v1.schema.json",
    "assurance.intake.result.case-review.v1": "result-contracts/case-review.v1.schema.json",
    "assurance.intake.result.explore.v1": "result-contracts/explore.v1.schema.json",
    "assurance.intake.result.intake.v1": "result-contracts/intake.v1.schema.json",
    "assurance.intake.skill.aa-case-design.case-delta-reviewer-prompt.v1": (
        "skills/aa-case-design/case-delta-reviewer-prompt.md"
    ),
    "assurance.intake.skill.aa-case-design.v1": "skills/aa-case-design/SKILL.md",
    "assurance.intake.skill.aa-case-design.visual-companion.v1": (
        "skills/aa-case-design/visual-companion.md"
    ),
    "assurance.intake.skill.aa-case-repair.v1": "skills/aa-case-repair/SKILL.md",
    "assurance.intake.skill.aa-case-reviewer.v1": "skills/aa-case-reviewer/SKILL.md",
    "assurance.intake.skill.aa-explore.v1": "skills/aa-explore/SKILL.md",
    "assurance.intake.skill.aa-intake.v1": "skills/aa-intake/SKILL.md",
}

_SCHEMA_FILES: dict[str, str] = {
    "assurance.intake.schema.resolved-assurance-plan.v1": ("schemas/resolved-assurance-plan.v1.schema.json"),
    "assurance.intake.schema.case-authoring.v1": "schemas/case-authoring.v1.schema.json",
    "assurance.intake.schema.case-review.v1": "schemas/case-review.v1.schema.json",
    "assurance.intake.schema.case.v1": "schemas/case.v1.schema.json",
    "assurance.intake.schema.case-selection.v1": "schemas/case-selection.v1.schema.json",
    "assurance.intake.schema.qa-change.v1": "schemas/qa-change.v1.schema.json",
    "assurance.intake.workflow.case.input.v1": "schemas/workflow/case-input.v1.schema.json",
    "assurance.intake.workflow.case.output.v1": "schemas/workflow/case-output.v1.schema.json",
    "assurance.intake.workflow.prepare.input.v1": "schemas/workflow/prepare-input.v1.schema.json",
    "assurance.intake.workflow.prepare.output.v1": "schemas/workflow/prepare-output.v1.schema.json",
}

_HANDLERS = {
    "assurance.intake.case-design.finalize": CaseDesignFinalizeHandler(),
    "assurance.intake.case-design.prepare": CaseDesignPrepareHandler(),
    "assurance.intake.case-review.finalize": CaseReviewFinalizeHandler(),
    "assurance.intake.case-review.prepare": CaseReviewPrepareHandler(),
    "assurance.intake.explore.finalize": ExploreFinalizeHandler(),
    "assurance.intake.explore.prepare": ExplorePrepareHandler(),
    "assurance.intake.intake.finalize": IntakeFinalizeHandler(),
    "assurance.intake.intake.prepare": IntakePrepareHandler(),
    "assurance.intake.review-round.advance": ReviewRoundAdvanceHandler(),
    "assurance.intake.resolve-plan": ResolvePlanHandler(),
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
        resource_bytes=resource_bytes,
        schema_files=_SCHEMA_FILES,
        resource_files=INTAKE_RESOURCE_FILES,
        task_handlers=_HANDLERS,
        commit_validators=_VALIDATORS,
    )

    @classmethod
    def descriptor(cls) -> PluginDescriptor:
        return cls.spec.descriptor().model_copy(update={"attempt_contracts": attempt_contract_refs()})

    @classmethod
    def contribute(cls, ports: RegistryPorts) -> PluginContribution:
        return replace(cls.spec.contribution(ports), attempt_contracts=attempt_contract_refs())
