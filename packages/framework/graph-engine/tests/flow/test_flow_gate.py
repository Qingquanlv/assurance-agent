from __future__ import annotations

from typing import Literal

from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command
from pydantic import BaseModel

from graph_engine.artifacts import ArtifactRef
from graph_engine.flow import Flow
from graph_engine.stategraph.ledger import NamedWrite
from graph_engine.testing import committed

from support import CONFIG, RECEIPT, ChangeInput, MarkerOutput, Toy, contract, open_harness


class _Decision(BaseModel):
    action: Literal["approve", "reject", "request_rework"]
    approval_ref: str = ""


class _ApplyInput(BaseModel):
    change_id: str = "c1"
    approval_ref: str


class _Handle:
    ledger_key = "lane.note"


async def test_gate_payload_resume_and_field_are_visible_to_the_next_step() -> None:
    propose = contract("propose")
    apply = contract("apply", input_model=_ApplyInput)
    flow = Flow("repair", input=ChangeInput, outcomes=("applied", "rejected", "failed"))
    flow.step(
        "propose",
        Toy(propose, writes=(NamedWrite(name="note", root="qa/note.md"),)),
        on_failure="failed",
        then="approval",
    )
    approval = flow.gate(
        "approval",
        decision=_Decision,
        routes={"approve": "apply", "reject": "rejected", "request_rework": "rejected"},
        show=(_Handle(),),
        interrupt_id="fix-proposal-approval",
    )
    flow.step(
        "apply",
        apply,
        on_failure="failed",
        then="applied",
        inputs={"approval_ref": approval.field("approval_ref")},
    )
    harness, context = open_harness(propose, apply)
    graph = flow.compile(context)
    graph.checkpointer = MemorySaver()
    note = ArtifactRef(path="qa/note.md", digest="a" * 64)
    harness._kernel.load_script(
        {
            "lane.propose": [committed(MarkerOutput(), RECEIPT, artifacts=(note,))],
            "lane.apply": [committed(MarkerOutput(), RECEIPT)],
        }
    )

    paused = await graph.ainvoke({"change_id": "c1"}, config=CONFIG)
    payload = paused["__interrupt__"][0].value
    assert set(payload) == {"reason", "actions", "interrupt_id", "ordinal", "rounds", "show"}
    assert payload["reason"] == "approval"
    assert payload["actions"] == ["approve", "reject", "request_rework"]
    assert payload["interrupt_id"] == "fix-proposal-approval"
    assert payload["ordinal"] == 0
    assert payload["show"]["lane.note"][0]["path"] == "qa/note.md"

    finished = await graph.ainvoke(
        Command(resume={"action": "approve", "approval_ref": "ref-1"}), config=CONFIG
    )
    assert finished["flow_outcome"] == "applied"
    assert [call.semantic_node_id for call in harness._kernel.semantic_calls] == [
        "lane.propose",
        "lane.apply",
    ]
    assert harness._context is not None
    assert harness._context.select_values[1]["approval_ref"] == "ref-1"


async def test_a_gate_can_advance_a_loop_without_rerunning_a_committed_step() -> None:
    draft = contract("draft")

    class _Rework(BaseModel):
        action: Literal["approve", "request_rework"]

    flow = Flow("case", input=ChangeInput, outcomes=("done", "exhausted", "failed"))
    with flow.loop("review", budget=2, on_exhausted="exhausted") as review:
        flow.step("draft", draft, on_failure="failed", then="human")
        flow.gate(
            "human",
            decision=_Rework,
            routes={"approve": "done", "request_rework": review.next("draft")},
        )
    harness, context = open_harness(draft)
    graph = flow.compile(context)
    graph.checkpointer = MemorySaver()
    harness._kernel.load_script(
        {"lane.draft": [committed(MarkerOutput(), RECEIPT), committed(MarkerOutput(), RECEIPT)]}
    )

    first = await graph.ainvoke({"change_id": "c1"}, config=CONFIG)
    assert first["__interrupt__"][0].value["rounds"] == {"review": 0}
    second = await graph.ainvoke(Command(resume="request_rework"), config=CONFIG)
    assert second["__interrupt__"][0].value["rounds"] == {"review": 1}
    finished = await graph.ainvoke(Command(resume="approve"), config=CONFIG)

    assert finished["flow_outcome"] == "done"
    assert [call.semantic_node_id for call in harness._kernel.semantic_calls] == ["lane.draft", "lane.draft"]
