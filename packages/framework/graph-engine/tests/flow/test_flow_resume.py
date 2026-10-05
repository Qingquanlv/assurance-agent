from __future__ import annotations

from typing import Literal

from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command
from pydantic import BaseModel

from graph_engine.attempts import node_factory
from graph_engine.attempts.resolutions import PendingTaskResult, SystemReference
from graph_engine.flow import Flow
from graph_engine.testing import committed

from support import CONFIG, RECEIPT, ChangeInput, MarkerOutput, contract, open_harness


class _Approval(BaseModel):
    action: Literal["approve", "reject"]


def _calls(harness: object) -> list[str]:
    return [call.semantic_node_id for call in getattr(harness, "_kernel").semantic_calls]


async def test_a_gate_inside_a_subflow_resumes_without_rerunning_the_child() -> None:
    propose = contract("propose")
    apply = contract("apply")
    child = Flow("repair", input=ChangeInput, outcomes=("applied", "rejected", "failed"))
    child.step("propose", propose, on_failure="failed", then="approval")
    child.gate("approval", decision=_Approval, routes={"approve": "apply", "reject": "rejected"})
    child.step("apply", apply, on_failure="failed", then="applied")
    parent = Flow("tail", input=ChangeInput, outcomes=("done", "stopped", "failed"))
    parent.subflow(
        "repair",
        child,
        routes={"applied": "done", "rejected": "stopped", "failed": "failed"},
    )
    harness, context = open_harness(propose, apply)
    graph = parent.compile(context)
    graph.checkpointer = MemorySaver()
    harness._kernel.load_script(
        {
            "lane.propose": [committed(MarkerOutput(), RECEIPT)],
            "lane.apply": [committed(MarkerOutput(), RECEIPT)],
        }
    )

    paused = await graph.ainvoke({"change_id": "c1"}, config=CONFIG)
    assert paused["__interrupt__"][0].value["interrupt_id"] == "tail.approval"
    finished = await graph.ainvoke(Command(resume="approve"), config=CONFIG)

    assert finished["flow_outcome"] == "done"
    assert finished["flow_control"]["results"]["repair"] == "applied"
    assert _calls(harness) == ["lane.propose", "lane.apply"]


async def test_a_gate_in_one_branch_resumes_without_rerunning_the_other_branch() -> None:
    api_task = contract("api")
    e2e_task = contract("e2e")
    api = Flow("api", input=ChangeInput, outcomes=("passed", "rejected", "failed"))
    api.step("codegen", api_task, on_failure="failed", then="human")
    api.gate("human", decision=_Approval, routes={"approve": "passed", "reject": "rejected"})
    e2e = Flow("e2e", input=ChangeInput, outcomes=("passed", "failed"))
    e2e.step("codegen", e2e_task, on_failure="failed", then="passed")
    flow = Flow("generation", input=ChangeInput, outcomes=("passed", "failed"))
    flow.parallel(
        "families",
        branches={"api": api, "e2e": e2e},
        select=None,
        require="passed",
        then="passed",
        on_failure="failed",
    )
    harness, context = open_harness(api_task, e2e_task)
    graph = flow.compile(context)
    graph.checkpointer = MemorySaver()
    harness._kernel.load_script(
        {
            "lane.api.codegen": [committed(MarkerOutput(), RECEIPT)],
            "lane.e2e.codegen": [committed(MarkerOutput(), RECEIPT)],
        }
    )

    paused = await graph.ainvoke({"change_id": "c1"}, config=CONFIG)
    assert paused["__interrupt__"][0].value["interrupt_id"] == "generation.api.human"
    finished = await graph.ainvoke(Command(resume="approve"), config=CONFIG)

    assert finished["flow_outcome"] == "passed"
    assert finished["flow_control"]["results"] == {"api": "passed", "e2e": "passed"}
    assert sorted(_calls(harness)) == ["lane.api.codegen", "lane.e2e.codegen"]


class _Hold(BaseModel):
    route: Literal["again", "hold"]


async def test_parallel_branch_gates_resume_apart_with_isolated_rounds() -> None:
    def lane(name: str):
        task = contract(name, output_model=_Hold)
        flow = Flow(name, input=ChangeInput, outcomes=("passed", "rejected", "exhausted", "failed"))
        with flow.loop("review", budget=3, on_exhausted="exhausted") as review:
            flow.step(
                "mark",
                task,
                on_failure="failed",
                route_on="route",
                routes={"again": review.next("mark"), "hold": "human"},
            )
            flow.gate(
                "human",
                decision=_Approval,
                routes={"approve": "passed", "reject": "rejected"},
            )
        return task, flow

    api_task, api = lane("api")
    e2e_task, e2e = lane("e2e")
    flow = Flow("generation", input=ChangeInput, outcomes=("passed", "failed"))
    flow.parallel(
        "families",
        branches={"api": api, "e2e": e2e},
        select=None,
        require="passed",
        then="passed",
        on_failure="failed",
    )
    harness, context = open_harness(api_task, e2e_task)
    activations: list[str] = []
    derive = node_factory.derive_attempt_key

    def _record(**kwargs: object) -> object:
        business = kwargs["business_activation"]
        activations.append(str(getattr(business, "value")))
        return derive(**kwargs)  # type: ignore[arg-type]

    node_factory.derive_attempt_key = _record  # type: ignore[assignment]
    try:
        graph = flow.compile(context)
        graph.checkpointer = MemorySaver()
        harness._kernel.load_script(
            {
                "lane.api.mark": [
                    committed(_Hold(route="again"), RECEIPT),
                    committed(_Hold(route="hold"), RECEIPT),
                ],
                "lane.e2e.mark": [
                    committed(_Hold(route="again"), RECEIPT),
                    committed(_Hold(route="hold"), RECEIPT),
                ],
            }
        )

        paused = await graph.ainvoke({"change_id": "c1"}, config=CONFIG)
        pending = {item.value["interrupt_id"]: item.id for item in paused["__interrupt__"]}
        assert set(pending) == {"generation.api.human", "generation.e2e.human"}
        assert any("b-api" in value and "review-0" in value for value in activations)
        assert any("b-api" in value and "review-1" in value for value in activations)
        assert any("b-e2e" in value and "review-0" in value for value in activations)
        assert any("b-e2e" in value and "review-1" in value for value in activations)
        assert len(set(activations)) == 4

        still = await graph.ainvoke(
            Command(resume={pending["generation.api.human"]: "approve"}),
            config=CONFIG,
        )
        assert [item.value["interrupt_id"] for item in still["__interrupt__"]] == ["generation.e2e.human"]
        assert len(activations) == 4

        finished = await graph.ainvoke(
            Command(resume={pending["generation.e2e.human"]: "reject"}),
            config=CONFIG,
        )
        assert finished["flow_outcome"] == "failed"
        assert finished["flow_control"]["results"] == {"api": "passed", "e2e": "rejected"}
        assert len(activations) == 4
    finally:
        node_factory.derive_attempt_key = derive


async def test_a_system_interrupt_inside_a_subflow_step_resumes_to_the_commit() -> None:
    run = contract("run")
    child = Flow("execute", input=ChangeInput, outcomes=("committed", "failed"))
    child.step("run", run, on_failure="failed", then="committed")
    parent = Flow("tail", input=ChangeInput, outcomes=("done", "failed"))
    parent.subflow("execute", child, routes={"committed": "done", "failed": "failed"})
    harness, context = open_harness(run)
    graph = parent.compile(context)
    graph.checkpointer = MemorySaver()
    harness._kernel.load_script(
        {
            "lane.run": [
                PendingTaskResult(wakeup=SystemReference(reference_id="wake-1")),
                committed(MarkerOutput(), RECEIPT),
            ]
        }
    )

    paused = await graph.ainvoke({"change_id": "c1"}, config=CONFIG)
    assert paused["__interrupt__"][0].value["kind"] == "system_wake"
    finished = await graph.ainvoke(Command(resume=True), config=CONFIG)

    assert finished["flow_outcome"] == "done"
    assert _calls(harness) == ["lane.run", "lane.run"]
