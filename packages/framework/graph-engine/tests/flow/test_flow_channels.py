from __future__ import annotations

from typing import Literal

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command
from pydantic import BaseModel

from graph_engine.attempts.models.resolutions import PermanentTaskFailure
from graph_engine.flow import Flow, FlowCheckError
from graph_engine.testing import committed

from support import CONFIG, RECEIPT, ChangeInput, MarkerOutput, contract, open_harness, state_of


class _Verdict(BaseModel):
    public_outcome: Literal["pass", "needs_human", "needs_fix"]


class _Decision(BaseModel):
    action: Literal["approve", "reject", "request_rework"]


class _Pair(BaseModel):
    action: Literal["approve", "reject"]


async def test_root_output_hides_inputs_and_can_mirror_the_outcome() -> None:
    step = contract("step")
    flow = Flow("lane", input=ChangeInput, outcomes=("done", "failed"))
    flow.step("step", step, on_failure="failed", then="done")
    harness, context = open_harness(step)
    graph = flow.compile(context, outcome_field="status")

    result = await harness.run(
        graph, input={"change_id": "c1"}, script={"lane.step": [committed(MarkerOutput(), RECEIPT)]}
    )
    finished = state_of(result)

    assert finished["flow_outcome"] == "done"
    assert finished["status"] == "done"
    assert "change_id" not in finished


def test_reserved_names_cannot_be_inputs_exports_or_the_outcome_field() -> None:
    step = contract("step")

    class _Reserved(BaseModel):
        flow_outcome: str = "x"

    flow = Flow("lane", input=_Reserved, outcomes=("done", "failed"))
    flow.step("step", step, on_failure="failed", then="done")
    with pytest.raises(FlowCheckError, match="collides"):
        _compile(flow, step)

    controlled = Flow("lane", input=ChangeInput, outcomes=("done", "failed"))
    controlled.step("step", step, on_failure="failed", then="done")
    controlled.control("step", flow_outcome="marker")
    with pytest.raises(FlowCheckError, match="collides"):
        _compile(controlled, step)

    mirrored = Flow("lane", input=ChangeInput, outcomes=("done", "failed"))
    mirrored.step("step", step, on_failure="failed", then="done")
    with pytest.raises(FlowCheckError, match="collides"):
        mirrored.compile(open_harness(step)[1], outcome_field="change_id")


def _compile(flow: Flow, *tasks: object) -> None:
    flow.compile(open_harness(*tasks)[1])  # type: ignore[arg-type]


async def test_a_gate_hears_only_the_failure_that_routed_directly_to_it() -> None:
    work = contract("work")
    judge = contract("judge", output_model=_Verdict)
    flow = Flow("case", input=ChangeInput, outcomes=("done", "rejected", "exhausted", "failed"))
    with flow.loop("review", budget=2, on_exhausted="exhausted") as review:
        flow.step("work", work, on_failure="failed", then="judge")
        flow.step(
            "judge",
            judge,
            on_failure="human",
            route_on="public_outcome",
            routes={
                "pass": "done",
                "needs_human": "human",
                "needs_fix": review.next("work"),
            },
        )
        flow.gate(
            "human",
            decision=_Decision,
            routes={
                "approve": "done",
                "reject": "rejected",
                "request_rework": review.next("work"),
            },
        )
    harness, context = open_harness(work, judge)
    graph = flow.compile(context)
    graph.checkpointer = MemorySaver()
    harness._kernel.load_script(
        {
            "lane.work": [committed(MarkerOutput(), RECEIPT), committed(MarkerOutput(), RECEIPT)],
            "lane.judge": [
                PermanentTaskFailure(kind="invalid_output", message="bad review"),
                committed(_Verdict(public_outcome="needs_human"), RECEIPT),
            ],
        }
    )

    failed = await graph.ainvoke({"change_id": "c1"}, config=CONFIG)
    failure = failed["__interrupt__"][0].value["failure"]
    assert failure == {
        "node": "lane.judge",
        "resolution_kind": "permanent",
        "kind": "invalid_output",
        "message": "bad review",
    }

    again = await graph.ainvoke(Command(resume="request_rework"), config=CONFIG)
    payload = again["__interrupt__"][0].value
    assert "failure" not in payload
    assert payload["reason"] == "human"

    finished = await graph.ainvoke(Command(resume="approve"), config=CONFIG)
    assert finished["flow_outcome"] == "done"


async def test_a_gate_reached_from_another_gate_has_no_failure() -> None:
    boom = contract("boom")
    flow = Flow("pair", input=ChangeInput, outcomes=("done", "rejected"))
    flow.step("boom", boom, on_failure="first", then="done")
    flow.gate("first", decision=_Pair, routes={"approve": "second", "reject": "rejected"})
    flow.gate("second", decision=_Pair, routes={"approve": "done", "reject": "rejected"})
    harness, context = open_harness(boom)
    graph = flow.compile(context)
    graph.checkpointer = MemorySaver()
    harness._kernel.load_script({"lane.boom": [PermanentTaskFailure(kind="invalid_output", message="nope")]})

    first = await graph.ainvoke({"change_id": "c1"}, config=CONFIG)
    assert first["__interrupt__"][0].value["failure"]["node"] == "lane.boom"
    second = await graph.ainvoke(Command(resume="approve"), config=CONFIG)
    assert "failure" not in second["__interrupt__"][0].value
