from __future__ import annotations

import pytest
from pydantic import BaseModel

from graph_engine.flow import Flow, FlowCheckError, const
from graph_engine.testing import committed

from support import (
    RECEIPT,
    ChangeInput,
    MarkerOutput,
    Toy,
    calls_of,
    contract,
    open_harness,
    spy_keys,
    state_of,
)


class _ChildInput(BaseModel):
    n: int
    label: str


class _Digest(BaseModel):
    plan_digest: str


def test_subflow_routes_must_cover_every_child_outcome() -> None:
    child = Flow("child", input=ChangeInput, outcomes=("ok", "failed"))
    child.step("run", contract("run"), on_failure="failed", then="ok")
    parent = Flow("parent", input=ChangeInput, outcomes=("done", "failed"))
    parent.step("start", contract("start"), on_failure="failed", then="lane")
    parent.subflow("lane", child, routes={"ok": "done"})
    _harness, context = open_harness(contract("run"), contract("start"))
    with pytest.raises(FlowCheckError, match="routes must cover"):
        parent.compile(context)


async def test_subflow_inputs_bind_const_and_loop_round() -> None:
    child_task = contract("run", input_model=_ChildInput)
    child = Flow("child", input=_ChildInput, outcomes=("ok", "failed"))
    child.step("run", child_task, on_failure="failed", then="ok")
    parent = Flow("parent", input=ChangeInput, outcomes=("done", "exhausted"))
    with parent.loop("cover", budget=1, on_exhausted="exhausted") as cover:
        parent.subflow(
            "lane",
            child,
            inputs={"n": cover.round, "label": const("x")},
            routes={"ok": "done", "failed": "exhausted"},
        )
    harness, context = open_harness(child_task)
    result = await harness.run(
        parent.compile(context),
        input={},
        script={"lane.run": [committed(MarkerOutput(), RECEIPT)]},
    )

    assert state_of(result)["flow_outcome"] == "done"
    assert result.select_values[0] == {"n": 0, "label": "x"}


async def test_mounting_a_flow_does_not_change_semantic_ids_or_share_attempt_keys() -> None:
    child_task = contract("case-review")
    child = Flow("case", input=ChangeInput, outcomes=("ok", "failed"))
    child.step("case-review", Toy(child_task, namespace="intake"), on_failure="failed", then="ok")
    parent = Flow("product", input=ChangeInput, outcomes=("done", "failed"))
    parent.subflow("left", child, routes={"ok": "right", "failed": "failed"})
    parent.subflow("right", child, routes={"ok": "done", "failed": "failed"})
    harness, context = open_harness(child_task)
    keys = spy_keys(harness)
    result = await harness.run(
        parent.compile(context),
        input={},
        script={
            "intake.case-review": [
                committed(MarkerOutput(marker="L"), RECEIPT),
                committed(MarkerOutput(marker="R"), RECEIPT),
            ]
        },
    )

    assert state_of(result)["flow_outcome"] == "done"
    assert calls_of(result) == ["intake.case-review", "intake.case-review"]
    assert keys[0].digest != keys[1].digest


async def test_subflow_controls_pass_through_to_the_parent() -> None:
    task = contract("resolve", output_model=_Digest)
    child = Flow("prepare", input=ChangeInput, outcomes=("prepared", "failed"))
    child.step("resolve", task, on_failure="failed", then="prepared")
    child.control("resolve", plan_digest="plan_digest")
    parent = Flow("product", input=ChangeInput, outcomes=("done", "failed"))
    parent.subflow("prepare", child, routes={"prepared": "done", "failed": "failed"})
    harness, context = open_harness(task)
    result = await harness.run(
        parent.compile(context),
        input={},
        script={"lane.resolve": [committed(_Digest(plan_digest="digest-1"), RECEIPT)]},
    )

    assert state_of(result)["plan_digest"] == "digest-1"
    assert state_of(result)["flow_outcome"] == "done"
