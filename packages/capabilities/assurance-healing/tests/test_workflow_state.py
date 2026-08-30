from __future__ import annotations

from typing import cast

import pytest

from graph_engine.canonical import JSONValue
from graph_engine.plugin_api import TaskOutcome

from tests.product.test_change_local_output_routing import execute_task

_KINDS = ("failure", "coverage")
_ADVANCE_ID = "assurance.healing.repair-round.advance"


def _handler():
    from assurance_healing.operations.workflow_state import HealingRepairRoundAdvanceHandler

    return HealingRepairRoundAdvanceHandler()


async def _advance(payload: dict[str, object]) -> TaskOutcome:
    executed = await execute_task(
        _handler(),
        cast(JSONValue, payload),
        capability_id=_ADVANCE_ID,
    )
    return executed.outcome


@pytest.mark.parametrize("kind", _KINDS)
@pytest.mark.parametrize(("used", "budget", "expected_used"), [(0, 2, 1), (1, 2, 2)])
async def test_repair_round_advance_increments_once_and_preserves_kind_budget(
    kind: str,
    used: int,
    budget: int,
    expected_used: int,
) -> None:
    outcome = await _advance({"kind": kind, "rounds_used": used, "rounds_budget": budget})
    assert outcome.status == "succeeded"
    assert outcome.output == {
        "kind": kind,
        "rounds_used": expected_used,
        "rounds_budget": budget,
    }
    assert outcome.effects == ()


@pytest.mark.parametrize("kind", _KINDS)
@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "failure", "rounds_used": 2, "rounds_budget": 2},
        {"kind": "failure", "rounds_used": -1, "rounds_budget": 2},
        {"kind": "failure", "rounds_used": 0, "rounds_budget": 0},
        {"kind": "failure", "rounds_used": 3, "rounds_budget": 2},
        {"kind": "failure", "rounds_used": 1},
        {"kind": "coverage", "rounds_used": 2, "rounds_budget": 2},
        {"kind": "unknown", "rounds_used": 0, "rounds_budget": 2},
        {"kind": "failure", "rounds_used": 0, "rounds_budget": 2, "extra": True},
    ],
)
async def test_repair_round_advance_rejects_invalid_counters_without_output(
    kind: str,
    payload: dict[str, object],
) -> None:
    del kind
    outcome = await _advance(payload)
    assert outcome.status == "failed"
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_input"
    assert outcome.failure.retryable is False
    assert outcome.output is None
    assert outcome.effects == ()
