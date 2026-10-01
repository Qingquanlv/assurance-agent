"""Prepare entry kept for callers that invoke the apply-test-repair module directly."""

from __future__ import annotations

from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest

from assurance_healing.ops.apply_test_repair import op


async def execute(request: TaskRequest, context: TaskContext) -> TaskOutcome:
    return op.handle_prepare(request, context)
