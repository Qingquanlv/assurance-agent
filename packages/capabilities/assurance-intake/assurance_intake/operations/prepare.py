"""Common Intake prepare handlers and Agent request construction."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from agent_runtime_contracts import AgentRunRequest, ResultContract
from agent_runtime_contracts.ops import (
    AgentBindingDataV1,
    InputError,
    failed_input,
    prepared_outcome,
    result_contract_from,
    skill_request,
    validate_binding,
    validate_model,
)
from graph_engine.canonical import canonical_json_bytes
from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest

from assurance_intake.contracts.agent import (
    CaseReviewInputV1,
    ExploreInputV1,
)
from assurance_intake.contracts.explore import EXPLORE_AGENT_OUTPUT_PATHS
from assurance_intake.operations.explore_context import build_explore_context
from assurance_intake.operations.planning_facts import build_planning_facts
from assurance_intake.resource_loader import resource_bytes, resource_text

from assurance_intake.operations.prepare_evidence import (
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


def result_contract(schema_id: str) -> ResultContract:
    return result_contract_from(schema_id, json.loads(resource_bytes(_RESULT_FILES[schema_id])))


def prepare_request(
    *,
    skill_path: str,
    persona_path: str,
    business: Any,
    binding: AgentBindingDataV1,
    result_schema_id: str,
    context: TaskContext,
    allowed_outputs: tuple[str, ...],
    planning_facts: dict[str, Any] | None = None,
) -> AgentRunRequest:
    return skill_request(
        skill_text=resource_text(skill_path),
        persona_text=resource_text(persona_path),
        business=business,
        business_extra=None if planning_facts is None else {"planning_facts": planning_facts},
        binding=binding,
        result=result_contract(result_schema_id),
        roots=context,
        allowed_outputs=allowed_outputs,
        scope_id=business.change_id,
    )


def prepare_outcome(**kwargs: Any) -> TaskOutcome:
    return prepared_outcome(prepare_request(**kwargs))


def materialize_requirement(text: str) -> bytes:
    return (text.removesuffix("\n") + "\n").encode("utf-8")


def write_prepare_file(write_root: Path, relative: str, data: bytes) -> None:
    path = write_root.joinpath(*relative.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


class ExplorePrepareHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            business = validate_model(ExploreInputV1, request.input)
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
            business = validate_model(CaseReviewInputV1, request.input)
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
