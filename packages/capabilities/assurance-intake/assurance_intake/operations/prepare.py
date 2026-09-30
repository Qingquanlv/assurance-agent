"""Common Intake prepare handlers and Agent request construction."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from agent_runtime_contracts import (
    AgentRunRequest,
    AgentWorkspaceV1,
    InstructionPart,
    ResultContract,
    prompt_model_json,
    with_validation_retry,
)
from agent_runtime_contracts.schema import canonical_digest
from graph_engine.canonical import canonical_json_bytes
from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest

from assurance_intake.contracts.agent import (
    AgentBindingDataV1,
    CaseReviewInputV1,
    ExploreInputV1,
    IntakeInputV1,
)
from assurance_intake.contracts.explore import (
    EXPLORE_AGENT_OUTPUT_PATHS,
    REQUIREMENT_PATH,
    RUN_SPEC_SNAPSHOT_PATH,
)
from assurance_intake.operations.explore_context import build_explore_context
from assurance_intake.operations.planning_facts import build_planning_facts
from assurance_intake.resource_loader import resource_bytes, resource_text

from assurance_intake.operations.prepare_evidence import (
    InputError,
    _authenticate_evidence_refs,
    _authenticate_plan,
    _require_regular_project_input,
)

INTAKE_SKILL = "skills/aa-intake/SKILL.md"
INTAKE_PERSONA = "personas/intake-host.md"
EXPLORE_SKILL = "skills/aa-explore/SKILL.md"
EXPLORE_PERSONA = "personas/explorer.md"
CASE_DESIGN_SKILL = "skills/aa-case-design/SKILL.md"
CASE_DESIGN_REPAIR_SKILL = "skills/aa-case-repair/SKILL.md"
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
    del change_id
    return ("qa/.qa.yaml",)


def explore_outputs(change_id: str) -> tuple[str, ...]:
    del change_id
    return EXPLORE_AGENT_OUTPUT_PATHS


def case_design_outputs(change_id: str, case_delta_paths: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        sorted(
            (
                *case_delta_paths,
                "qa/.qa.yaml",
                "qa/proposal.md",
                "qa/results/trace/minimum-coverage-matrix.json",
            )
        )
    )


def case_review_outputs(
    change_id: str,
    *,
    coverage_epoch: int = 0,
    review_round: int = 0,
) -> tuple[str, ...]:
    del change_id, coverage_epoch, review_round
    return tuple(
        sorted(
            (
                "qa/results/review/case-review.json",
                "qa/results/review/case-review-summary.md",
            )
        )
    )


def case_review_inputs(change_id: str, case_delta_paths: tuple[str, ...]) -> tuple[str, ...]:
    change_root = "qa"
    return tuple(
        sorted(
            (
                f"{change_root}/.qa.yaml",
                *case_delta_paths,
                f"{change_root}/proposal.md",
                f"{change_root}/requirement.md",
                f"{change_root}/results/trace/minimum-coverage-matrix.json",
            )
        )
    )


def _logical_write_root(context: TaskContext) -> str:
    try:
        relative = context.write_root.resolve().relative_to(context.project_root.resolve()).as_posix()
    except ValueError:
        relative = "qa/.staging/write"
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
        "read_roots": (),
    }
    return AgentWorkspaceV1.model_validate({**payload, "identity_digest": canonical_digest(payload)})


def result_contract(schema_id: str) -> ResultContract:
    payload = json.loads(resource_bytes(_RESULT_FILES[schema_id]))
    return ResultContract(
        schema_id=schema_id,
        schema_digest=canonical_digest(payload),
        delivery_mode="assistant_json_local_v1",
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
    planning_facts: dict[str, Any] | None = None,
) -> TaskOutcome:
    business_input = prompt_model_json(business)
    if planning_facts is not None:
        business_input["planning_facts"] = planning_facts
    agent_request = AgentRunRequest(
        instructions=with_validation_retry(
            (
                InstructionPart.text("text/plain", resource_text(skill_path)),
                InstructionPart.text("text/plain", resource_text(persona_path)),
                InstructionPart.from_json(business_input),
            ),
            getattr(business, "validation_error", None),
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
    return TaskOutcome.failed("invalid_input", str(error), retryable=True)


def _materialize_requirement(text: str) -> bytes:
    return (text.removesuffix("\n") + "\n").encode("utf-8")


def _write_prepare_file(write_root: Path, relative: str, data: bytes) -> None:
    path = write_root.joinpath(*relative.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


class IntakePrepareHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            business = validate_input(IntakeInputV1, request.input)
            binding = validate_binding(request.binding_data)
            _write_prepare_file(
                context.write_root, REQUIREMENT_PATH, _materialize_requirement(business.requirement)
            )
            snapshot = yaml.safe_dump(
                {"candidate_test_families": list(business.candidate_test_families)},
                sort_keys=True,
            ).encode("utf-8")
            _write_prepare_file(context.write_root, RUN_SPEC_SNAPSHOT_PATH, snapshot)
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
                capability_leafs=business.capability_leafs,
            )
            relative = "qa/results/explore/context.json"
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
        except (InputError, ValueError) as error:
            return failed_input(error)


class CaseReviewPrepareHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            business = validate_input(CaseReviewInputV1, request.input)
            binding = validate_binding(request.binding_data)
            plan = _authenticate_plan(
                context.project_root,
                change_id=business.change_id,
                plan_digest=business.plan_digest,
                plan_ref=business.plan_ref,
            )
            _authenticate_evidence_refs(context.project_root, business.preparation_refs)
            _authenticate_evidence_refs(context.project_root, business.case_refs)
            if business.case_delta_paths and not set(business.case_delta_paths) <= {
                item.path for item in business.case_refs
            }:
                raise InputError("case_refs must bind every locked case_delta_path")
            review_inputs = case_review_inputs(business.change_id, business.case_delta_paths)
            for relative in review_inputs:
                _require_regular_project_input(context.project_root, relative)
            business = CaseReviewInputV1.model_validate(
                {**business.model_dump(mode="json"), "review_input_paths": review_inputs}
            )
            return prepare_outcome(
                skill_path=CASE_REVIEW_SKILL,
                persona_path=CASE_REVIEW_PERSONA,
                business=business,
                binding=binding,
                result_schema_id=CASE_REVIEW_RESULT_ID,
                context=context,
                allowed_outputs=case_review_outputs(
                    business.change_id,
                    coverage_epoch=business.coverage_epoch,
                    review_round=business.review_round,
                ),
                planning_facts=build_planning_facts(
                    context.project_root,
                    change_id=business.change_id,
                    capability_leafs=business.capability_leafs,
                    families=plan.selected_test_families,
                ),
            )
        except InputError as error:
            return failed_input(error)
