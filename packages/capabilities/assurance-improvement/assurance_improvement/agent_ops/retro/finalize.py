"""Finalize the retro synthesis Agent result."""

from __future__ import annotations

from agent_runtime_contracts.ops import run_finalize
from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest

from assurance_improvement.contracts.agent import RetroSynthesisFinalizeInputV1
from assurance_improvement.operations.agent import commit_retro

input_model = RetroSynthesisFinalizeInputV1


def _commit(payload: RetroSynthesisFinalizeInputV1, context: TaskContext) -> TaskOutcome:
    return commit_retro(payload, context, expected_domain=None)


async def execute(request: TaskRequest, context: TaskContext) -> TaskOutcome:
    return run_finalize(request, context, input_model=RetroSynthesisFinalizeInputV1, commit=_commit)
