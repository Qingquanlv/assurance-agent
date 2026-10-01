"""Prepare the retro issue-analysis Agent request."""

from __future__ import annotations

from agent_runtime_contracts import AgentRunRequest
from agent_runtime_contracts.ops import AgentBindingDataV1, run_prepare
from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest

from assurance_improvement.contracts.agent import RetroAnalysisInputV1
from assurance_improvement.operations.agent import RETRO_ISSUE_SKILL, RETRO_RESULT_ID, prepare_request
from assurance_improvement.operations.common import validate_input


def _build(
    business: RetroAnalysisInputV1,
    binding: AgentBindingDataV1,
    context: TaskContext,
) -> AgentRunRequest:
    return prepare_request(
        skill_path=RETRO_ISSUE_SKILL,
        business=business,
        binding=binding,
        result_schema_id=RETRO_RESULT_ID,
        context=context,
    )


async def execute(request: TaskRequest, context: TaskContext) -> TaskOutcome:
    return run_prepare(
        request, context, input_model=RetroAnalysisInputV1, build=_build, validate=validate_input
    )
