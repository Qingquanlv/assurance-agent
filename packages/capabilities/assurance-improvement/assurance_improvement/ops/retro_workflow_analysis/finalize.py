"""Finalize entry kept for callers that invoke this op module directly."""

from __future__ import annotations

from agent_runtime_contracts.ops import AgentOpFinalizeInputV1
from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest

from assurance_improvement.ops.retro_workflow_analysis import op

input_model = AgentOpFinalizeInputV1


async def execute(request: TaskRequest, context: TaskContext) -> TaskOutcome:
    return op.handle_finalize(request, context)
