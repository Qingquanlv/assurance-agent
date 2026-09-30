"""Prepare the archive Agent request."""

from __future__ import annotations

from agent_runtime_contracts import AgentRunRequest
from agent_runtime_contracts.ops import AgentBindingDataV1, run_prepare
from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest

from assurance_improvement.contracts.agent import ImprovementSkillInputV1
from assurance_improvement.operations.agent import ARCHIVE_RESULT_ID, ARCHIVE_SKILL, prepare_request


def _build(
    business: ImprovementSkillInputV1,
    binding: AgentBindingDataV1,
    context: TaskContext,
) -> AgentRunRequest:
    return prepare_request(
        skill_path=ARCHIVE_SKILL,
        business=business,
        binding=binding,
        result_schema_id=ARCHIVE_RESULT_ID,
        context=context,
    )


async def execute(request: TaskRequest, context: TaskContext) -> TaskOutcome:
    return run_prepare(request, context, input_model=ImprovementSkillInputV1, build=_build)
