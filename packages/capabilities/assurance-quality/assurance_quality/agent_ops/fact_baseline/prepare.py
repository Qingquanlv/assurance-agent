"""Prepare the fact-baseline Agent request."""

from __future__ import annotations

from agent_runtime_contracts import AgentRunRequest
from agent_runtime_contracts.ops import AgentBindingDataV1, InputError, OutputError, run_prepare
from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest

from assurance_quality.contracts.assessment import FactBaselineSkillInputV1
from assurance_quality.operations.agent_skills import (
    FACT_BASELINE_RESULT_ID,
    FACT_BASELINE_SKILL,
    authenticate_fact_baseline_input,
    prepare_request,
)


def _build(
    business: FactBaselineSkillInputV1,
    binding: AgentBindingDataV1,
    context: TaskContext,
) -> AgentRunRequest:
    authenticate_fact_baseline_input(business, context.project_root)
    return prepare_request(
        skill_path=FACT_BASELINE_SKILL,
        business=business,
        binding=binding,
        result_schema_id=FACT_BASELINE_RESULT_ID,
        context=context,
    )


async def execute(request: TaskRequest, context: TaskContext) -> TaskOutcome:
    return run_prepare(
        request,
        context,
        input_model=FactBaselineSkillInputV1,
        build=_build,
        input_errors=(InputError, OutputError),
    )
