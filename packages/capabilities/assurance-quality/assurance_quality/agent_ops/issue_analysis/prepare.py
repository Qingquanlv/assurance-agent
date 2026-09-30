"""Prepare the issue-analysis Agent request."""

from __future__ import annotations

from agent_runtime_contracts import AgentRunRequest
from agent_runtime_contracts.ops import AgentBindingDataV1, InputError, OutputError, run_prepare
from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest

from assurance_quality.contracts.agent import QualitySkillInputV1
from assurance_quality.operations.agent_skills import (
    ISSUE_ANALYSIS_RESULT_ID,
    ISSUE_ANALYSIS_SKILL,
    authenticate_issue_analysis_input,
    prepare_request,
)


def _build(
    business: QualitySkillInputV1,
    binding: AgentBindingDataV1,
    context: TaskContext,
) -> AgentRunRequest:
    authenticate_issue_analysis_input(business, context.project_root)
    return prepare_request(
        skill_path=ISSUE_ANALYSIS_SKILL,
        business=business,
        binding=binding,
        result_schema_id=ISSUE_ANALYSIS_RESULT_ID,
        context=context,
    )


async def execute(request: TaskRequest, context: TaskContext) -> TaskOutcome:
    return run_prepare(
        request,
        context,
        input_model=QualitySkillInputV1,
        build=_build,
        input_errors=(InputError, OutputError),
    )
