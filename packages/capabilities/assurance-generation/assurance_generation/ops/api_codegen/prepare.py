"""Prepare entry kept for callers that invoke the api.codegen module directly."""

from __future__ import annotations

from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest

from assurance_generation.ops.api_codegen import op


async def execute(request: TaskRequest, context: TaskContext) -> TaskOutcome:
    return op.handle_prepare(request, context)
