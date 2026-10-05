from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from graph_engine.flow import Flow
from graph_engine.testing import committed

from support import RECEIPT, ChangeInput, calls_of, contract, open_harness, spy_keys, state_of


class _Budgets(BaseModel):
    review_rounds: int


class _BudgetInput(BaseModel):
    budgets: _Budgets


class _Route(BaseModel):
    route: Literal["pass", "fix", "human"]


class _Again(BaseModel):
    route: Literal["again", "done"]


class _RoundInput(BaseModel):
    n: int


async def test_a_case_shaped_loop_has_two_next_targets_and_distinct_keys() -> None:
    design = contract("design")
    review = contract("review", output_model=_Route)
    repair = contract("repair")
    flow = Flow("case", input=ChangeInput, outcomes=("reviewed", "exhausted", "failed"))
    with flow.loop("review", budget=3, on_exhausted="exhausted") as review_loop:
        flow.step("design", design, on_failure="failed", then="case-review")
        flow.step(
            "case-review",
            review,
            on_failure="failed",
            route_on="route",
            routes={
                "pass": "reviewed",
                "fix": review_loop.next("repair"),
                "human": review_loop.next("design"),
            },
        )
        flow.step("repair", repair, on_failure="failed", then="case-review")
    harness, context = open_harness(design, review, repair)
    keys = spy_keys(harness)
    result = await harness.run(
        flow.compile(context),
        input={"change_id": "c1"},
        script={
            "lane.design": [committed(_Route_marker(), RECEIPT), committed(_Route_marker(), RECEIPT)],
            "lane.case-review": [
                committed(_Route(route="human"), RECEIPT),
                committed(_Route(route="fix"), RECEIPT),
                committed(_Route(route="pass"), RECEIPT),
            ],
            "lane.repair": [committed(_Route_marker(), RECEIPT)],
        },
    )

    assert state_of(result)["flow_outcome"] == "reviewed"
    assert calls_of(result) == [
        "lane.design",
        "lane.case-review",
        "lane.design",
        "lane.case-review",
        "lane.repair",
        "lane.case-review",
    ]
    assert len({key.digest for key in keys}) == len(keys)


class _Marker(BaseModel):
    marker: str = "ok"


def _Route_marker() -> _Marker:
    return _Marker()


async def test_integer_and_path_budgets_exit_through_on_exhausted() -> None:
    work = contract("work", output_model=_Again)
    integer = Flow("integer", input=ChangeInput, outcomes=("done", "exhausted", "failed"))
    with integer.loop("review", budget=1, on_exhausted="exhausted") as review:
        integer.step(
            "work",
            work,
            on_failure="failed",
            route_on="route",
            routes={"again": review.next("work"), "done": "done"},
        )
    harness, context = open_harness(work)
    keys = spy_keys(harness)
    result = await harness.run(
        integer.compile(context),
        input={},
        script={
            "lane.work": [
                committed(_Again(route="again"), RECEIPT),
                committed(_Again(route="again"), RECEIPT),
            ]
        },
    )
    assert state_of(result)["flow_outcome"] == "exhausted"
    assert len(calls_of(result)) == 2
    assert keys[0].digest != keys[1].digest

    path = Flow("path", input=_BudgetInput, outcomes=("done", "exhausted", "failed"))
    with path.loop("review", budget="budgets.review_rounds", on_exhausted="exhausted") as review:
        path.step(
            "work",
            work,
            on_failure="failed",
            route_on="route",
            routes={"again": review.next("work"), "done": "done"},
        )
    harness, context = open_harness(work)
    result = await harness.run(
        path.compile(context),
        input={"budgets": {"review_rounds": 1}},
        script={
            "lane.work": [
                committed(_Again(route="again"), RECEIPT),
                committed(_Again(route="again"), RECEIPT),
            ]
        },
    )
    assert state_of(result)["flow_outcome"] == "exhausted"
    assert len(calls_of(result)) == 2


async def test_reentering_an_inner_loop_resets_its_round_without_reusing_keys() -> None:
    work = contract("work", input_model=_RoundInput, output_model=_Again)
    inner = Flow("inner", input=ChangeInput, outcomes=("done", "exhausted", "failed"))
    with inner.loop("review", budget=2, on_exhausted="exhausted") as review:
        inner.step(
            "work",
            work,
            on_failure="failed",
            route_on="route",
            routes={"again": review.next("work"), "done": "done"},
            inputs={"n": review.round},
        )
    outer = Flow("outer", input=ChangeInput, outcomes=("exhausted", "failed"))
    with outer.loop("cover", budget=1, on_exhausted="exhausted") as cover:
        outer.subflow(
            "lane",
            inner,
            routes={"done": cover.next("lane"), "exhausted": "exhausted", "failed": "failed"},
        )
    harness, context = open_harness(work)
    keys = spy_keys(harness)
    result = await harness.run(
        outer.compile(context),
        input={},
        script={
            "lane.work": [
                committed(_Again(route="again"), RECEIPT),
                committed(_Again(route="done"), RECEIPT),
                committed(_Again(route="done"), RECEIPT),
            ]
        },
    )

    assert state_of(result)["flow_outcome"] == "exhausted"
    assert calls_of(result) == ["lane.work", "lane.work", "lane.work"]
    assert [item["n"] for item in result.select_values] == [0, 1, 0]
    assert keys[0].digest != keys[2].digest
    assert len({key.digest for key in keys}) == 3
