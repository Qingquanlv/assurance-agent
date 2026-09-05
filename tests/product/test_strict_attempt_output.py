from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from graph_engine.attempts import ExecutedAttemptResult
from graph_engine.canonical import JSONValue
from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest
from assurance_improvement.contracts.attempts import ClosedImprovementExecutor
from assurance_product.runtime_bindings import DeterministicTaskExecutor
from tests.product.test_semantic_attempt_bindings import _phase_scope


class Result(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: int


class ResultHandler:
    def __init__(self, payload: JSONValue) -> None:
        self.payload = payload

    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del request, context
        return TaskOutcome.succeeded(self.payload)


@pytest.mark.parametrize("executor_type", [DeterministicTaskExecutor, ClosedImprovementExecutor])
@pytest.mark.parametrize("wrapper", ["status", "projection"])
def test_attempt_output_rejects_wrapped_results(
    tmp_path: Path,
    executor_type: type[DeterministicTaskExecutor] | type[ClosedImprovementExecutor],
    wrapper: str,
) -> None:
    scope = _phase_scope(tmp_path)
    handler = ResultHandler({"value": 7})
    executor = executor_type("assurance.improvement.test-output", handler, Result)
    result = asyncio.run(executor.execute(Result(value=7), scope))
    assert isinstance(result, ExecutedAttemptResult)
    assert result.output == Result(value=7)

    wrapped: JSONValue = {wrapper: {"value": 7}}
    handler.payload = wrapped
    with pytest.raises(ValidationError):
        asyncio.run(executor.execute(Result(value=7), scope))
