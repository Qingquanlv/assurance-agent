"""Prepare the improvement-review Agent request."""

from __future__ import annotations

from agent_runtime_contracts import AgentRunRequest
from agent_runtime_contracts.ops import AgentBindingDataV1, run_prepare
from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest

from assurance_improvement.contracts.agent import ImprovementSkillInputV1
from assurance_improvement.operations.agent import REVIEW_RESULT_ID, REVIEW_SKILL, prepare_request
from assurance_improvement.operations.common import validate_input


def _build(
    business: ImprovementSkillInputV1,
    binding: AgentBindingDataV1,
    context: TaskContext,
) -> AgentRunRequest:
    return prepare_request(
        skill_path=REVIEW_SKILL,
        business=business,
        binding=binding,
        result_schema_id=REVIEW_RESULT_ID,
        context=context,
    )


async def execute(request: TaskRequest, context: TaskContext) -> TaskOutcome:
    return run_prepare(
        request, context, input_model=ImprovementSkillInputV1, build=_build, validate=validate_input
    )
