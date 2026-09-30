"""Prepare the report Agent request."""

from __future__ import annotations

from agent_runtime_contracts import AgentRunRequest
from agent_runtime_contracts.ops import AgentBindingDataV1, InputError, OutputError, run_prepare
from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest

from assurance_quality.contracts.assessment import ReportSkillInputV1
from assurance_quality.operations.agent_skills import (
    REPORT_RESULT_ID,
    REPORT_SKILL,
    authenticate_report_input,
    prepare_request,
)


def _build(
    business: ReportSkillInputV1,
    binding: AgentBindingDataV1,
    context: TaskContext,
) -> AgentRunRequest:
    authenticate_report_input(business, context.project_root)
    return prepare_request(
        skill_path=REPORT_SKILL,
        business=business,
        binding=binding,
        result_schema_id=REPORT_RESULT_ID,
        context=context,
    )


async def execute(request: TaskRequest, context: TaskContext) -> TaskOutcome:
    return run_prepare(
        request,
        context,
        input_model=ReportSkillInputV1,
        build=_build,
        input_errors=(InputError, OutputError),
    )
