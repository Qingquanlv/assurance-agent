from __future__ import annotations

from typing import Literal

import pytest
from pydantic import BaseModel

from graph_engine.flow import Flow, FlowCheckError
from graph_engine.testing import committed

from support import RECEIPT, ChangeInput, calls_of, contract, open_harness, state_of


class _Route(BaseModel):
    route: Literal["left", "middle", "right"]


class _OptionalRoute(BaseModel):
    route: Literal["left", "middle", "right"] | None


class _PlainRoute(BaseModel):
    route: str


async def test_route_on_selects_one_of_three_literal_outcomes() -> None:
    step = contract("step", output_model=_Route)
    flow = Flow("routed", input=ChangeInput, outcomes=("left", "middle", "right", "failed"))
    flow.step(
        "step",
        step,
        on_failure="failed",
        route_on="route",
        routes={"left": "left", "middle": "middle", "right": "right"},
    )
    harness, context = open_harness(step)
    graph = flow.compile(context)

    result = await harness.run(
        graph,
        input={},
        script={"lane.step": [committed(_Route(route="middle"), RECEIPT)]},
    )

    assert state_of(result)["flow_outcome"] == "middle"
    assert calls_of(result) == ["lane.step"]


def _routed(output: type[BaseModel], routes: dict[str, str]) -> None:
    step = contract("step", output_model=output)
    flow = Flow("routed", input=ChangeInput, outcomes=("left", "middle", "right", "failed"))
    flow.step("step", step, on_failure="failed", route_on="route", routes=routes)
    _harness, context = open_harness(step)
    flow.compile(context)


def test_route_on_rejects_missing_extra_plain_and_optional_literals() -> None:
    with pytest.raises(FlowCheckError, match="must equal"):
        _routed(_Route, {"left": "left", "middle": "middle"})
    with pytest.raises(FlowCheckError, match="must equal"):
        _routed(_Route, {"left": "left", "middle": "middle", "right": "right", "extra": "failed"})
    with pytest.raises(FlowCheckError, match="must be a Literal"):
        _routed(_PlainRoute, {"left": "left"})
    with pytest.raises(FlowCheckError, match="must not be an optional Literal"):
        _routed(_OptionalRoute, {"left": "left", "middle": "middle", "right": "right"})
