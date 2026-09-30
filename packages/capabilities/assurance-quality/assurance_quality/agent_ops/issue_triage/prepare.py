"""Prepare the issue-triage Agent request."""

from __future__ import annotations

from agent_runtime_contracts import AgentRunRequest
from agent_runtime_contracts.ops import AgentBindingDataV1, run_prepare
from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest

from assurance_quality.contracts.agent import QualitySkillInputV1
from assurance_quality.operations.agent_skills import (
    ISSUE_TRIAGE_RESULT_ID,
    ISSUE_TRIAGE_SKILL,
    prepare_request,
)


def _build(
    business: QualitySkillInputV1,
    binding: AgentBindingDataV1,
    context: TaskContext,
) -> AgentRunRequest:
    return prepare_request(
        skill_path=ISSUE_TRIAGE_SKILL,
        business=business,
        binding=binding,
        result_schema_id=ISSUE_TRIAGE_RESULT_ID,
        context=context,
    )


async def execute(request: TaskRequest, context: TaskContext) -> TaskOutcome:
    return run_prepare(request, context, input_model=QualitySkillInputV1, build=_build)
