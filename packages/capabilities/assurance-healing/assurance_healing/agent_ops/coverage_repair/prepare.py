"""Prepare the coverage-repair Agent request."""

from __future__ import annotations

from agent_runtime_contracts import AgentRunRequest
from agent_runtime_contracts.ops import AgentBindingDataV1, run_prepare
from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest

from assurance_healing.contracts.agent import CoverageRepairInputV1
from assurance_healing.operations.agent import (
    COVERAGE_REPAIR_RESULT_ID,
    COVERAGE_REPAIR_SKILL,
    REPAIR_RESULT_FILE,
    prepare_request,
)


def _build(
    business: CoverageRepairInputV1,
    binding: AgentBindingDataV1,
    context: TaskContext,
) -> AgentRunRequest:
    return prepare_request(
        skill_path=COVERAGE_REPAIR_SKILL,
        business=business,
        binding=binding,
        result_id=COVERAGE_REPAIR_RESULT_ID,
        result_file=REPAIR_RESULT_FILE,
        context=context,
        allowed_outputs=("qa/results/healing/coverage-repair.json",),
    )


async def execute(request: TaskRequest, context: TaskContext) -> TaskOutcome:
    return run_prepare(request, context, input_model=CoverageRepairInputV1, build=_build)
