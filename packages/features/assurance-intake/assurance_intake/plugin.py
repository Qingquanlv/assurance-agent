from __future__ import annotations

from graph_engine import ENGINE_API_VERSION
from graph_engine.plugin_api import (
    PluginContribution,
    PluginDescriptor,
    ProviderSource,
    RegistryPorts,
    ResourceContribution,
    SchemaContribution,
)

from assurance_intake.operations import (
    CaseDesignFinalizeHandler,
    CaseDesignPrepareHandler,
    CaseReviewFinalizeHandler,
    CaseReviewPrepareHandler,
    ExploreFinalizeHandler,
    ExplorePrepareHandler,
    IntakeFinalizeHandler,
    IntakePrepareHandler,
    ReviewRoundAdvanceHandler,
)
from assurance_intake.resource_loader import resource_bytes
from assurance_intake.validators import CaseCandidateValidator, CaseReferenceValidator

INTAKE_SOURCE = ProviderSource(
    distribution="assurance-intake",
    version="0.1.0",
    entrypoint_group="graph_engine.plugins",
    entrypoint_name="intake",
    entrypoint_value="assurance_intake.plugin:IntakePlugin",
    declaration_path="assurance_intake/plugin-declaration.json",
    import_roots=("",),
)

INTAKE_HANDLER_IDS: tuple[str, ...] = (
    "assurance.intake.case-design.finalize",
    "assurance.intake.case-design.prepare",
    "assurance.intake.case-review.finalize",
    "assurance.intake.case-review.prepare",
    "assurance.intake.explore.finalize",
    "assurance.intake.explore.prepare",
    "assurance.intake.intake.finalize",
    "assurance.intake.intake.prepare",
    "assurance.intake.review-round.advance",
)

INTAKE_VALIDATOR_IDS: tuple[str, ...] = (
    "assurance.intake.validator.case-candidate.v1",
    "assurance.intake.validator.case-references.v1",
)

INTAKE_SCHEMA_IDS: tuple[str, ...] = (
    "assurance.intake.schema.case-authoring.v1",
    "assurance.intake.schema.case-review.v1",
    "assurance.intake.schema.case.v1",
    "assurance.intake.schema.qa-change.v1",
    "assurance.intake.workflow.case.input.v1",
    "assurance.intake.workflow.case.output.v1",
    "assurance.intake.workflow.prepare.input.v1",
    "assurance.intake.workflow.prepare.output.v1",
)

INTAKE_RESOURCE_FILES: dict[str, str] = {
    "assurance.intake.persona.doc-author.v1": "personas/doc-author.md",
    "assurance.intake.persona.explorer.v1": "personas/explorer.md",
    "assurance.intake.persona.intake-host.v1": "personas/intake-host.md",
    "assurance.intake.persona.reviewer.v1": "personas/reviewer.md",
    "assurance.intake.prompt.case-design.v1": "prompts/case-design.md",
    "assurance.intake.prompt.case-review.v1": "prompts/case-review.md",
    "assurance.intake.prompt.explore.v1": "prompts/explore.md",
    "assurance.intake.prompt.intake.v1": "prompts/intake.md",
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
    "assurance.intake.skill.aa-case-reviewer.v1": "skills/aa-case-reviewer/SKILL.md",
    "assurance.intake.skill.aa-explore.v1": "skills/aa-explore/SKILL.md",
    "assurance.intake.skill.aa-intake.v1": "skills/aa-intake/SKILL.md",
    "assurance.intake.workflow.module.v1": "workflow/module.yaml",
}

INTAKE_RESOURCE_IDS: tuple[str, ...] = tuple(sorted(INTAKE_RESOURCE_FILES))

_SCHEMA_FILES: dict[str, str] = {
    "assurance.intake.schema.case-authoring.v1": "schemas/case-authoring.v1.schema.json",
    "assurance.intake.schema.case-review.v1": "schemas/case-review.v1.schema.json",
    "assurance.intake.schema.case.v1": "schemas/case.v1.schema.json",
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
}

_VALIDATORS = {
    "assurance.intake.validator.case-candidate.v1": CaseCandidateValidator(),
    "assurance.intake.validator.case-references.v1": CaseReferenceValidator(),
}


def _schema_contributions() -> tuple[SchemaContribution, ...]:
    return tuple(
        SchemaContribution(
            schema_id=schema_id,
            media_type="application/schema+json",
            content=resource_bytes(_SCHEMA_FILES[schema_id]),
        )
        for schema_id in INTAKE_SCHEMA_IDS
    )


_WORKFLOW_MODULE_MIME = "application/vnd.graph-engine.workflow-module+yaml"


def _resource_media_type(path: str) -> str:
    if path.endswith(".schema.json"):
        return "application/schema+json"
    if path.endswith("workflow/module.yaml"):
        return _WORKFLOW_MODULE_MIME
    return "text/plain"


def _resource_contributions() -> tuple[ResourceContribution, ...]:
    return tuple(
        ResourceContribution(
            resource_id=resource_id,
            media_type=_resource_media_type(INTAKE_RESOURCE_FILES[resource_id]),
            content=resource_bytes(INTAKE_RESOURCE_FILES[resource_id]),
        )
        for resource_id in INTAKE_RESOURCE_IDS
    )


class IntakePlugin:
    @staticmethod
    def descriptor() -> PluginDescriptor:
        return PluginDescriptor(
            schema_version="1",
            source=INTAKE_SOURCE,
            plugin_id="assurance.intake",
            plugin_version="0.1.0",
            engine_api=ENGINE_API_VERSION,
            task_handlers=INTAKE_HANDLER_IDS,
            commit_validators=INTAKE_VALIDATOR_IDS,
            schemas=INTAKE_SCHEMA_IDS,
            resources=INTAKE_RESOURCE_IDS,
        )

    @staticmethod
    def contribute(ports: RegistryPorts) -> PluginContribution:
        if ports.engine_api != ENGINE_API_VERSION:
            raise ValueError(f"unsupported engine API: {ports.engine_api!r}")
        return PluginContribution(
            task_handlers=_HANDLERS,
            commit_validators=_VALIDATORS,
            schemas=_schema_contributions(),
            resources=_resource_contributions(),
        )
