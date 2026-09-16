from __future__ import annotations

from typing import cast

import pytest

from graph_engine.canonical import JSONValue
from graph_engine.plugin_api import TaskOutcome

from tests.product.test_change_local_output_routing import execute_task


def test_review_round_advance_handler_is_yaml_adapter_for_pure_function() -> None:
    import inspect

    from assurance_intake.operations.workflow_state import ReviewRoundAdvanceHandler

    source = inspect.getsource(ReviewRoundAdvanceHandler.execute)
    assert "advance_review_round" in source
    assert "del context" in source


def _handler():
    from assurance_intake.operations.workflow_state import ReviewRoundAdvanceHandler

    return ReviewRoundAdvanceHandler()


async def _advance(payload: dict[str, object]) -> TaskOutcome:
    executed = await execute_task(
        _handler(),
        cast(JSONValue, payload),
        capability_id="assurance.intake.review-round.advance",
    )
    return executed.outcome


@pytest.mark.parametrize(
    ("used", "budget", "expected_used"),
    [(0, 2, 1), (1, 2, 2)],
)
async def test_review_round_advance_increments_within_budget(
    used: int, budget: int, expected_used: int
) -> None:
    outcome = await _advance({"rounds_used": used, "rounds_budget": budget})
    assert outcome.status == "succeeded"
    assert outcome.output == {"rounds_used": expected_used, "rounds_budget": budget}


@pytest.mark.parametrize(
    "payload",
    [
        {"rounds_used": 2, "rounds_budget": 2},
        {"rounds_used": -1, "rounds_budget": 2},
        {"rounds_used": 0, "rounds_budget": 0},
        {"rounds_used": 3, "rounds_budget": 2},
        {"rounds_used": 1},
        {"rounds_budget": 2},
        {"rounds_used": 0, "rounds_budget": 2, "extra": True},
    ],
)
async def test_review_round_advance_rejects_invalid_counters_without_output(
    payload: dict[str, object],
) -> None:
    outcome = await _advance(payload)
    assert outcome.status == "failed"
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_input"
    assert outcome.failure.retryable is True
    assert outcome.output is None
