"""Deterministic intake prepare handlers."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from agent_runtime_contracts import AgentRunRequest, AgentWorkspaceV1, InstructionPart, ResultContract
from agent_runtime_contracts.schema import canonical_digest
from graph_engine.canonical import canonical_json_bytes
from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest

from assurance_intake.contracts.agent import (
    AgentBindingDataV1,
    CaseDesignInputV1,
    CaseReviewInputV1,
    ExploreInputV1,
    IntakeInputV1,
)
from assurance_intake.contracts.explore import ExploreAdvisoryV1, build_explore_context
from assurance_intake.resource_loader import resource_bytes, resource_text

INTAKE_SKILL = "skills/aa-intake/SKILL.md"
INTAKE_PERSONA = "personas/intake-host.md"
EXPLORE_SKILL = "skills/aa-explore/SKILL.md"
EXPLORE_PERSONA = "personas/explorer.md"
CASE_DESIGN_SKILL = "skills/aa-case-design/SKILL.md"
CASE_DESIGN_PERSONA = "personas/doc-author.md"
CASE_REVIEW_SKILL = "skills/aa-case-reviewer/SKILL.md"
CASE_REVIEW_PERSONA = "personas/reviewer.md"

INTAKE_RESULT_ID = "assurance.intake.result.intake.v1"
EXPLORE_RESULT_ID = "assurance.intake.result.explore.v1"
CASE_DESIGN_RESULT_ID = "assurance.intake.result.case-design.v1"
CASE_REVIEW_RESULT_ID = "assurance.intake.result.case-review.v1"

_RESULT_FILES: Mapping[str, str] = {
    INTAKE_RESULT_ID: "result-contracts/intake.v1.schema.json",
    EXPLORE_RESULT_ID: "result-contracts/explore.v1.schema.json",
    CASE_DESIGN_RESULT_ID: "result-contracts/case-design.v1.schema.json",
    CASE_REVIEW_RESULT_ID: "result-contracts/case-review.v1.schema.json",
}
_BOUNDED_PROFILES: Mapping[str, str] = {
    "aa-archiver": "assurance-v1-archiver",
    "aa-doc-author": "assurance-v1-doc-author",
    "aa-executor": "assurance-v1-executor",
    "aa-explorer": "assurance-v1-explorer",
    "aa-reporter": "assurance-v1-reporter",
    "aa-reviewer": "assurance-v1-reviewer",
    "aa-test-author": "assurance-v1-test-author",
}


def intake_outputs(change_id: str) -> tuple[str, ...]:
    return tuple(sorted((f"qa/changes/{change_id}/.qa.yaml", f"qa/changes/{change_id}/requirement.md")))


def explore_outputs(change_id: str) -> tuple[str, ...]:
    return (f"qa/changes/{change_id}/explore/exploration.json",)


def case_design_outputs(change_id: str, case_delta_paths: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        sorted(
            (
                *case_delta_paths,
                f"qa/changes/{change_id}/.qa.yaml",
                f"qa/changes/{change_id}/proposal.md",
                f"qa/changes/{change_id}/trace/minimum-coverage-matrix.json",
            )
        )
    )


def case_review_outputs(change_id: str) -> tuple[str, ...]:
    return tuple(
        sorted(
            (
                f"qa/changes/{change_id}/review/case-review.json",
                f"qa/changes/{change_id}/review/case-review-summary.md",
            )
        )
    )


def _logical_write_root(context: TaskContext) -> str:
    try:
        relative = context.write_root.resolve().relative_to(context.project_root.resolve()).as_posix()
    except ValueError:
        relative = "qa/changes/_attempt/.staging/write"
    if relative in {".", ""}:
        return ".staging/write"
    return relative


def agent_workspace(
    context: TaskContext,
    *,
    allowed_outputs: tuple[str, ...],
    agent_profile: str,
    scope_id: str,
) -> AgentWorkspaceV1:
    write_root = _logical_write_root(context)
    payload = {
        "schema_version": "1",
        "agent_profile": _BOUNDED_PROFILES.get(agent_profile, agent_profile),
        "scope_id": scope_id,
        "write_root": write_root,
        "allowed_outputs": tuple(sorted(set(allowed_outputs))),
    }
    return AgentWorkspaceV1.model_validate({**payload, "identity_digest": canonical_digest(payload)})


class InputError(ValueError):
    """Malformed caller input or missing locked configuration."""


def result_contract(schema_id: str) -> ResultContract:
    payload = json.loads(resource_bytes(_RESULT_FILES[schema_id]))
    return ResultContract(
        schema_id=schema_id,
        schema_digest=canonical_digest(payload),
        extraction_mode="structured",
        schema_document=payload,
    )


def validate_binding(data: object) -> AgentBindingDataV1:
    try:
        return AgentBindingDataV1.model_validate(data)
    except ValidationError as error:
        raise InputError(str(error)) from error


def validate_input(model: type[Any], data: object) -> Any:
    try:
        return model.model_validate(data)
    except ValidationError as error:
        raise InputError(str(error)) from error


def prepare_outcome(
    *,
    skill_path: str,
    persona_path: str,
    business: Any,
    binding: AgentBindingDataV1,
    result_schema_id: str,
    context: TaskContext,
    allowed_outputs: tuple[str, ...],
) -> TaskOutcome:
    agent_request = AgentRunRequest(
        instructions=(
            InstructionPart.text("text/plain", resource_text(skill_path)),
            InstructionPart.text("text/plain", resource_text(persona_path)),
            InstructionPart.from_json(business.model_dump(mode="json")),
        ),
        result_contract=result_contract(result_schema_id),
        execution=binding.execution,
        workspace=agent_workspace(
            context,
            allowed_outputs=allowed_outputs,
            agent_profile=binding.agent_profile,
            scope_id=business.change_id,
        ),
        request_policy_digest=binding.request_policy_digest,
        request_config_digest=binding.request_config_digest,
    )
    return TaskOutcome.succeeded(agent_request.model_dump(mode="json"))


def failed_input(error: Exception) -> TaskOutcome:
    return TaskOutcome.failed("invalid_input", str(error), retryable=False)


class IntakePrepareHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            business = validate_input(IntakeInputV1, request.input)
            binding = validate_binding(request.binding_data)
            return prepare_outcome(
                skill_path=INTAKE_SKILL,
                persona_path=INTAKE_PERSONA,
                business=business,
                binding=binding,
                result_schema_id=INTAKE_RESULT_ID,
                context=context,
                allowed_outputs=intake_outputs(business.change_id),
            )
        except InputError as error:
            return failed_input(error)


class ExplorePrepareHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            business = validate_input(ExploreInputV1, request.input)
            binding = validate_binding(request.binding_data)
            document = build_explore_context(
                context.project_root,
                change_id=business.change_id,
            )
            relative = f"qa/changes/{business.change_id}/explore/context.json"
            path = context.write_root.joinpath(*relative.split("/"))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(canonical_json_bytes(document.model_dump(mode="json")) + b"\n")
            return prepare_outcome(
                skill_path=EXPLORE_SKILL,
                persona_path=EXPLORE_PERSONA,
                business=business,
                binding=binding,
                result_schema_id=EXPLORE_RESULT_ID,
                context=context,
                allowed_outputs=explore_outputs(business.change_id),
            )
        except InputError as error:
            return failed_input(error)


class CaseDesignPrepareHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            business = CaseDesignInputV1.model_validate(request.input)
            binding = AgentBindingDataV1.model_validate(request.binding_data)
            exploration_relative = f"qa/changes/{business.change_id}/explore/exploration.json"
            exploration_path = context.project_root.joinpath(*exploration_relative.split("/"))
            exploration = None
            if exploration_path.exists() or exploration_path.is_symlink():
                if not exploration_path.is_file() or exploration_path.is_symlink():
                    raise InputError("exploration.json must be a regular file")
                try:
                    exploration = ExploreAdvisoryV1.model_validate_json(exploration_path.read_bytes())
                except (OSError, ValidationError, ValueError) as error:
                    raise InputError(f"invalid exploration.json: {error}") from error
                if exploration.change_id != business.change_id:
                    raise InputError("exploration.json change_id does not match case-design change_id")
                if exploration.context_ref != "explore/context.json":
                    raise InputError("exploration.json context_ref must be explore/context.json")
            business = business.model_copy(update={"exploration": exploration})
            return prepare_outcome(
                skill_path=CASE_DESIGN_SKILL,
                persona_path=CASE_DESIGN_PERSONA,
                business=business,
                binding=binding,
                result_schema_id=CASE_DESIGN_RESULT_ID,
                context=context,
                allowed_outputs=case_design_outputs(
                    business.change_id,
                    business.case_delta_paths,
                ),
            )
        except (InputError, ValidationError) as error:
            return failed_input(error)


class CaseReviewPrepareHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            business = validate_input(CaseReviewInputV1, request.input)
            binding = validate_binding(request.binding_data)
            return prepare_outcome(
                skill_path=CASE_REVIEW_SKILL,
                persona_path=CASE_REVIEW_PERSONA,
                business=business,
                binding=binding,
                result_schema_id=CASE_REVIEW_RESULT_ID,
                context=context,
                allowed_outputs=case_review_outputs(business.change_id),
            )
        except InputError as error:
            return failed_input(error)
