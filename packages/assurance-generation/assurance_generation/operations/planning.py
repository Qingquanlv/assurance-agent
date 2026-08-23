"""Constructor-closed four-family plan prepare/finalize handlers."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Literal, cast

from pydantic import ValidationError

from agent_runtime_contracts import AgentRunRequest, InstructionPart, ResultContract
from agent_runtime_contracts.schema import canonical_digest
from graph_engine.canonical import JSONValue
from graph_engine.frozen_json import thaw_json
from graph_engine.plugin_api import TaskContext, TaskHandler, TaskOutcome, TaskRequest

from assurance_generation.contracts.agent import AgentBindingDataV1, AgentFinalizeInputV1, PlanInputV1
from assurance_generation.contracts.families import LAYER_NAMES, LayerName
from assurance_generation.contracts.plans import PlanResultV1, canonical_relative_path
from assurance_generation.resource_loader import resource_bytes, resource_text
from assurance_intake.contracts import CaseYamlAuthoring

Family = LayerName
FAMILIES: tuple[Family, ...] = LAYER_NAMES
PrepareKind = Literal["prepare", "finalize"]

PLAN_RESULT_ID = "assurance.generation.result.plan.v1"
PLAN_REVIEW_RESULT_ID = "assurance.generation.result.plan-review.v1"
_RESULT_FILES: Mapping[str, str] = {
    PLAN_RESULT_ID: "result-contracts/plan.v1.schema.json",
    PLAN_REVIEW_RESULT_ID: "result-contracts/plan-review.v1.schema.json",
}
_SKILL_FILES: Mapping[Family, str] = {
    "api": "skills/aa-api-plan/SKILL.md",
    "e2e": "skills/aa-e2e-plan/SKILL.md",
    "fuzz": "skills/aa-fuzz-plan/SKILL.md",
    "performance": "skills/aa-performance-plan/SKILL.md",
}
PLAN_PERSONA = "personas/test-author.md"
REVIEW_PERSONA = "personas/reviewer.md"


class InputError(ValueError):
    """Malformed caller input or missing locked configuration."""


class OutputError(ValueError):
    """Model-authored semantic invalidity."""


def closed_family(family: str) -> Family:
    if family not in FAMILIES:
        raise ValueError(f"unknown generation family: {family}")
    return cast(Family, family)


def result_contract(schema_id: str) -> ResultContract:
    payload = json.loads(resource_bytes(_RESULT_FILES[schema_id]))
    return ResultContract(
        schema_id=schema_id,
        schema_digest=canonical_digest(payload),
        extraction_mode="structured",
        schema_document=payload,
    )


def failed_input(error: Exception) -> TaskOutcome:
    return TaskOutcome.failed("invalid_input", str(error), retryable=False)


def failed_output(message: str) -> TaskOutcome:
    return TaskOutcome.failed("invalid_output", message, retryable=True)


def leafs_of(values: tuple[str, ...]) -> frozenset[str]:
    return frozenset(values)


def validate_plan_input(data: object) -> tuple[PlanInputV1, CaseYamlAuthoring]:
    try:
        business = PlanInputV1.model_validate(data)
        cases = CaseYamlAuthoring.model_validate(
            business.reviewed_cases,
            context={"capability_leafs": leafs_of(business.capability_leafs)},
        )
    except ValidationError as error:
        raise InputError(str(error)) from error
    return business, cases


def prepare_plan_outcome(
    *,
    family: Family,
    skill_path: str,
    persona_path: str,
    business: PlanInputV1,
    cases: CaseYamlAuthoring,
    binding: AgentBindingDataV1,
    result_schema_id: str,
) -> TaskOutcome:
    del family
    agent_request = AgentRunRequest(
        instructions=(
            InstructionPart.text("text/plain", resource_text(skill_path)),
            InstructionPart.text("text/plain", resource_text(persona_path)),
            InstructionPart.from_json(cases.model_dump(mode="json")),
            InstructionPart.from_json(business.family_constraints.model_dump(mode="json")),
        ),
        result_contract=result_contract(result_schema_id),
        execution=binding.execution,
        request_policy_digest=binding.request_policy_digest,
        request_config_digest=binding.request_config_digest,
    )
    return TaskOutcome.succeeded(agent_request.model_dump(mode="json"))


def _structured(payload: AgentFinalizeInputV1) -> object:
    return thaw_json(payload.agent_result.structured_result)


def _workspace_file(workspace: Path, relative: str) -> Path:
    try:
        canonical_relative_path(relative)
    except ValueError as error:
        raise OutputError(str(error)) from error
    path = workspace.joinpath(*PurePosixPath(relative).parts)
    if path.is_symlink():
        raise OutputError(f"declared output file is missing: {relative}")
    try:
        path.resolve().relative_to(workspace.resolve())
    except ValueError as error:
        raise OutputError(f"output file path must be canonical and relative: {relative}") from error
    if path.is_file() and not path.is_symlink() and path.stat().st_nlink != 1:
        raise OutputError(f"declared output file is not a regular single-link file: {relative}")
    return path


def _authenticate_files(
    workspace: Path,
    declared: tuple[str, ...],
    locked: tuple[str, ...],
) -> None:
    expected = set(locked)
    for relative in declared:
        if relative not in expected:
            raise OutputError(f"undeclared output file: {relative}")
        path = _workspace_file(workspace, relative)
        if not path.is_file() or path.is_symlink():
            raise OutputError(f"declared output file is missing: {relative}")


class PlanPrepareHandler:
    def __init__(self, family: Family) -> None:
        self._family: Family = closed_family(family)

    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            business, cases = validate_plan_input(request.input)
            binding = AgentBindingDataV1.model_validate(request.binding_data)
            return prepare_plan_outcome(
                family=self._family,
                skill_path=_SKILL_FILES[self._family],
                persona_path=PLAN_PERSONA,
                business=business,
                cases=cases,
                binding=binding,
                result_schema_id=PLAN_RESULT_ID,
            )
        except (InputError, ValidationError) as error:
            return failed_input(error)


class PlanFinalizeHandler:
    def __init__(self, family: Family) -> None:
        self._family: Family = closed_family(family)

    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            payload = AgentFinalizeInputV1.model_validate(request.input)
            try:
                document = PlanResultV1.model_validate(
                    _structured(payload),
                    context={"capability_leafs": leafs_of(payload.capability_leafs)},
                )
            except ValidationError as error:
                raise OutputError(str(error)) from error
            if document.family != self._family:
                raise OutputError(f"plan family {document.family!r} does not match {self._family}")
            if payload.artifact_paths:
                _authenticate_files(context.workspace_root, document.output_files, payload.artifact_paths)
            return TaskOutcome.succeeded(cast(JSONValue, document.model_dump(mode="json")))
        except ValidationError as error:
            return failed_input(error)
        except OutputError as error:
            return failed_output(str(error))


def planning_handler(family: str, kind: PrepareKind) -> TaskHandler:
    closed = closed_family(family)
    if kind == "prepare":
        return PlanPrepareHandler(closed)
    if kind == "finalize":
        return PlanFinalizeHandler(closed)
    raise ValueError(f"unknown planning handler kind: {kind}")


__all__ = [
    "FAMILIES",
    "PLAN_PERSONA",
    "PLAN_RESULT_ID",
    "PLAN_REVIEW_RESULT_ID",
    "REVIEW_PERSONA",
    "Family",
    "InputError",
    "OutputError",
    "PlanFinalizeHandler",
    "PlanPrepareHandler",
    "closed_family",
    "failed_input",
    "failed_output",
    "leafs_of",
    "planning_handler",
    "prepare_plan_outcome",
    "result_contract",
    "validate_plan_input",
]
