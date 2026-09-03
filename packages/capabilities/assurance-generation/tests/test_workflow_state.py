from __future__ import annotations

from typing import cast

import pytest

from graph_engine.canonical import JSONValue
from graph_engine.plugin_api import TaskOutcome

from tests.product.test_change_local_output_routing import execute_task

_FAMILIES = ("api", "e2e", "fuzz", "performance")
_STAGES = ("plan", "codegen")
_ADVANCE_ID = "assurance.generation.review-round.advance"
_COMPLETE_ID = "assurance.generation.complete"


def test_generation_handlers_are_yaml_adapters_for_pure_functions() -> None:
    import inspect

    from assurance_generation.operations.workflow_state import (
        GenerationCompleteHandler,
        GenerationReviewRoundAdvanceHandler,
    )

    complete_source = inspect.getsource(GenerationCompleteHandler.execute)
    advance_source = inspect.getsource(GenerationReviewRoundAdvanceHandler.execute)
    assert "complete_generation" in complete_source
    assert "advance_review_round" in advance_source
    assert "del context" in complete_source
    assert "del context" in advance_source


def _handler():
    from assurance_generation.operations.workflow_state import GenerationReviewRoundAdvanceHandler

    return GenerationReviewRoundAdvanceHandler()


async def _advance(payload: dict[str, object]) -> TaskOutcome:
    executed = await execute_task(
        _handler(),
        cast(JSONValue, payload),
        capability_id=_ADVANCE_ID,
    )
    return executed.outcome


async def _complete(payload: dict[str, object]) -> TaskOutcome:
    from assurance_generation.operations.workflow_state import GenerationCompleteHandler

    executed = await execute_task(
        GenerationCompleteHandler(),
        cast(JSONValue, payload),
        capability_id=_COMPLETE_ID,
    )
    return executed.outcome


async def test_generation_complete_emits_the_public_module_output() -> None:
    outcome = await _complete(
        {
            "completed": [{"value": True}] * 4,
            "selected_families": ["api", "fuzz"],
        }
    )
    assert outcome.status == "succeeded"
    assert outcome.output == {
        "families": {
            "api": {"completed": True},
            "fuzz": {"completed": True},
        },
        "selected_families": ["api", "fuzz"],
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"completed": [{"value": True}] * 3, "selected_families": ["api"]},
        {"completed": [{"value": True}] * 4, "selected_families": []},
        {"completed": [{"value": True}] * 4, "selected_families": ["api", "api"]},
        {"completed": [{"value": False}] * 4, "selected_families": ["api"]},
    ],
)
async def test_generation_complete_rejects_invalid_lane_state(payload: dict[str, object]) -> None:
    outcome = await _complete(payload)
    assert outcome.status == "failed"
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_input"


@pytest.mark.parametrize("family", _FAMILIES)
@pytest.mark.parametrize("stage", _STAGES)
@pytest.mark.parametrize(("used", "budget", "expected_used"), [(0, 2, 1), (1, 2, 2)])
async def test_review_round_advance_increments_once_and_preserves_family_stage_budget(
    family: str,
    stage: str,
    used: int,
    budget: int,
    expected_used: int,
) -> None:
    outcome = await _advance(
        {
            "family": family,
            "stage": stage,
            "rounds_used": used,
            "rounds_budget": budget,
        }
    )
    assert outcome.status == "succeeded"
    assert outcome.output == {
        "family": family,
        "stage": stage,
        "rounds_used": expected_used,
        "rounds_budget": budget,
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"family": "api", "stage": "plan", "rounds_used": 2, "rounds_budget": 2},
        {"family": "api", "stage": "plan", "rounds_used": -1, "rounds_budget": 2},
        {"family": "api", "stage": "plan", "rounds_used": 0, "rounds_budget": 0},
        {"family": "api", "stage": "plan", "rounds_used": 3, "rounds_budget": 2},
        {"family": "api", "stage": "plan", "rounds_used": 1},
        {"family": "unknown", "stage": "plan", "rounds_used": 0, "rounds_budget": 2},
        {"family": "api", "stage": "fix", "rounds_used": 0, "rounds_budget": 2},
        {"family": "api", "stage": "plan", "rounds_used": 0, "rounds_budget": 2, "extra": True},
    ],
)
async def test_review_round_advance_rejects_invalid_counters_without_output(
    payload: dict[str, object],
) -> None:
    outcome = await _advance(payload)
    assert outcome.status == "failed"
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_input"
    assert outcome.failure.retryable is False
    assert outcome.output is None


@pytest.mark.parametrize("family", _FAMILIES)
@pytest.mark.parametrize("stage", _STAGES)
async def test_two_lane_completions_in_different_order_produce_the_same_counters(
    family: str,
    stage: str,
) -> None:
    first = await _advance({"family": family, "stage": stage, "rounds_used": 0, "rounds_budget": 2})
    second = await _advance({"family": family, "stage": stage, "rounds_used": 1, "rounds_budget": 2})
    reverse_second = await _advance({"family": family, "stage": stage, "rounds_used": 1, "rounds_budget": 2})
    reverse_first = await _advance({"family": family, "stage": stage, "rounds_used": 0, "rounds_budget": 2})
    assert first.output == reverse_first.output
    assert second.output == reverse_second.output
    assert first.output == {
        "family": family,
        "stage": stage,
        "rounds_used": 1,
        "rounds_budget": 2,
    }
    assert second.output == {
        "family": family,
        "stage": stage,
        "rounds_used": 2,
        "rounds_budget": 2,
    }
