"""Prepare the case-review Agent request."""

from __future__ import annotations

from agent_runtime_contracts import AgentRunRequest
from agent_runtime_contracts.ops import AgentBindingDataV1, InputError, run_prepare
from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest

from assurance_intake.contracts.agent import CaseReviewInputV1
from assurance_intake.domain.planning_facts import build_planning_facts
from assurance_intake.operations.prepare import (
    CASE_REVIEW_PERSONA,
    CASE_REVIEW_RESULT_ID,
    CASE_REVIEW_SKILL,
    case_review_inputs,
    case_review_outputs,
    prepare_request,
)
from assurance_intake.operations.prepare_evidence import (
    authenticate_evidence_refs,
    authenticate_plan,
    require_regular_project_input,
)


def _build(business: CaseReviewInputV1, binding: AgentBindingDataV1, context: TaskContext) -> AgentRunRequest:
    plan = authenticate_plan(
        context.project_root,
        change_id=business.change_id,
        plan_digest=business.plan_digest,
        plan_ref=business.plan_ref,
    )
    authenticate_evidence_refs(context.project_root, business.preparation_refs)
    authenticate_evidence_refs(context.project_root, business.case_refs)
    if business.case_delta_paths and not set(business.case_delta_paths) <= {
        item.path for item in business.case_refs
    }:
        raise InputError("case_refs must bind every locked case_delta_path")
    review_inputs = case_review_inputs(business.change_id, business.case_delta_paths)
    for relative in review_inputs:
        require_regular_project_input(context.project_root, relative)
    business = CaseReviewInputV1.model_validate(
        {**business.model_dump(mode="json"), "review_input_paths": review_inputs}
    )
    return prepare_request(
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


async def execute(request: TaskRequest, context: TaskContext) -> TaskOutcome:
    return run_prepare(request, context, input_model=CaseReviewInputV1, build=_build)
