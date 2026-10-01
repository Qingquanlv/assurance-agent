"""Prepare entry kept for callers that invoke the e2e.codegen-review module directly."""

from __future__ import annotations

from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest

from assurance_generation.ops.e2e_codegen_review import op


async def execute(request: TaskRequest, context: TaskContext) -> TaskOutcome:
    return op.handle_prepare(request, context)
