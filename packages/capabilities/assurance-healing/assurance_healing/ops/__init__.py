"""Healing agent ops: one directory per op, dispatched through one execute entry."""

from __future__ import annotations

from agent_runtime_contracts.ops import AgentOpFinalizeInputV1, OpRouter
from graph_engine.attempts import AttemptRetryPolicy, AttemptTimeoutPolicy
from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest

router = OpRouter(
    "assurance.healing",
    package=__name__,
    reads=("qa",),
    agent_retry=AttemptRetryPolicy(max_attempts=10, interval_seconds=10),
    task_retry=AttemptRetryPolicy(max_attempts=1),
    timeout=AttemptTimeoutPolicy(seconds=60),
    scope=lambda business: business.change_id,
)
input_model = AgentOpFinalizeInputV1


async def execute(request: TaskRequest, context: TaskContext) -> TaskOutcome:
    return await router.execute(request, context)


__all__ = ["execute", "input_model", "router"]
