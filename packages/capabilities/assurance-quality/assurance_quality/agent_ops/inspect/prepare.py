"""Prepare the inspect Agent request."""

from __future__ import annotations

from agent_runtime_contracts import AgentRunRequest
from agent_runtime_contracts.ops import AgentBindingDataV1, InputError, OutputError, run_prepare
from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest

from assurance_quality.contracts.assessment import AssessmentSkillInputV1
from assurance_quality.operations.agent_skills import (
    INSPECTION_RESULT_ID,
    INSPECT_SKILL,
    authenticate_assessment_input,
    prepare_request,
)


def _build(
    business: AssessmentSkillInputV1,
    binding: AgentBindingDataV1,
    context: TaskContext,
) -> AgentRunRequest:
    authenticate_assessment_input(business, context.project_root)
    return prepare_request(
        skill_path=INSPECT_SKILL,
        business=business,
        binding=binding,
        result_schema_id=INSPECTION_RESULT_ID,
        context=context,
    )


async def execute(request: TaskRequest, context: TaskContext) -> TaskOutcome:
    return run_prepare(
        request,
        context,
        input_model=AssessmentSkillInputV1,
        build=_build,
        input_errors=(InputError, OutputError),
    )
