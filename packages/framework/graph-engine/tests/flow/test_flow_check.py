from __future__ import annotations

import pytest
from pydantic import BaseModel

from graph_engine.flow import Flow, FlowCheckError
from graph_engine.stategraph.ledger import InputBinding, NamedWrite

from support import ChangeInput, Toy, contract, open_harness


class _NeedsNote(BaseModel):
    note_ref: dict[str, str]


class _RoundInput(BaseModel):
    n: int


class _Handle:
    def __init__(self, ledger_key: str) -> None:
        self.ledger_key = ledger_key


def _compile(flow: Flow, *tasks: object) -> None:
    _harness, context = open_harness(*tasks)  # type: ignore[arg-type]
    flow.compile(context)


def test_checker_rejects_structure_inputs_and_nested_loops() -> None:
    step = contract("step")
    with pytest.raises(FlowCheckError, match="is not a flow name"):
        Flow("Case", input=ChangeInput, outcomes=("done",))
    flow = Flow("flow", input=ChangeInput, outcomes=("done", "failed"))
    with pytest.raises(FlowCheckError, match="is not a flow name"):
        flow.step("Step", step, on_failure="failed", then="done")

    flow = Flow("flow", input=ChangeInput, outcomes=("done", "failed"))
    flow.step("first", step, on_failure="failed", then="missing")
    with pytest.raises(FlowCheckError, match="unknown"):
        _compile(flow, step)

    flow = Flow("flow", input=ChangeInput, outcomes=("done", "other", "failed"))
    flow.step("first", step, on_failure="failed", then="done")
    with pytest.raises(FlowCheckError, match="unreachable outcome"):
        _compile(flow, step)

    flow = Flow("flow", input=ChangeInput, outcomes=("done", "failed"))
    flow.step("first", step, on_failure="failed", then="done")
    flow.step("second", step, on_failure="failed", then="done")
    with pytest.raises(FlowCheckError, match="unreachable node"):
        _compile(flow, step)

    needed = contract("needed", input_model=_NeedsNote)
    flow = Flow("flow", input=ChangeInput, outcomes=("done", "failed"))
    flow.step("first", needed, on_failure="failed", then="done")
    with pytest.raises(FlowCheckError, match="cannot be resolved"):
        _compile(flow, needed)

    reader = Toy(needed, bindings=(InputBinding(ledger_key="lane.note", field="note_ref"),))
    flow = Flow("flow", input=ChangeInput, outcomes=("done", "failed"))
    flow.step("first", reader, on_failure="failed", then="done")
    with pytest.raises(FlowCheckError, match="has no upstream writer"):
        _compile(flow, needed)
    supplied = Flow(
        "flow", input=ChangeInput, outcomes=("done", "failed"), ledger_inputs=(_Handle("lane.note"),)
    )
    supplied.step("first", reader, on_failure="failed", then="done")
    _compile(supplied, needed)

    duplicated = Toy(
        needed,
        bindings=(
            InputBinding(ledger_key="lane.note", field="note_ref"),
            InputBinding(ledger_key="lane.other", field="note_ref"),
        ),
    )
    flow = Flow(
        "flow",
        input=ChangeInput,
        outcomes=("done", "failed"),
        ledger_inputs=(_Handle("lane.note"), _Handle("lane.other")),
    )
    flow.step("first", duplicated, on_failure="failed", then="done")
    with pytest.raises(FlowCheckError, match="same input slot twice"):
        _compile(flow, needed)

    flow = Flow("flow", input=ChangeInput, outcomes=("done", "failed"))
    with pytest.raises(FlowCheckError, match="cannot nest"):
        with flow.loop("outer", budget=1, on_exhausted="done"):
            with flow.loop("inner", budget=1, on_exhausted="failed"):
                flow.step("first", step, on_failure="failed", then="done")


def test_checker_rejects_loop_round_outside_the_body_and_a_downstream_gate() -> None:
    step = contract("step", input_model=_RoundInput)
    flow = Flow("flow", input=ChangeInput, outcomes=("done", "failed"))
    with flow.loop("review", budget=1, on_exhausted="failed") as review:
        flow.step("inside", contract("inside"), on_failure="failed", then="outside")
    flow.step("outside", step, on_failure="failed", then="done", inputs={"n": review.round})
    with pytest.raises(FlowCheckError, match="outside loop"):
        _compile(flow, contract("inside"), step)

    from typing import Literal

    class _Decision(BaseModel):
        action: Literal["approve", "reject"]
        approval_ref: str = ""

    class _Apply(BaseModel):
        approval_ref: str

    apply = contract("apply", input_model=_Apply)
    gated = Flow("flow", input=ChangeInput, outcomes=("done", "failed"))
    approval = None
    gated.step("prep", contract("prep"), on_failure="failed", then="apply")
    approval = gated.gate(
        "approval",
        decision=_Decision,
        routes={"approve": "done", "reject": "failed"},
    )
    gated.step(
        "apply",
        apply,
        on_failure="failed",
        then="approval",
        inputs={"approval_ref": approval.field("approval_ref")},
    )
    with pytest.raises(FlowCheckError, match="is not upstream"):
        _compile(gated, contract("prep"), apply)


def test_an_upstream_write_satisfies_a_later_binding() -> None:
    writer_task = contract("writer")
    reader_task = contract("reader", input_model=_NeedsNote)
    writer = Toy(writer_task, writes=(NamedWrite(name="note", root="qa/note.md"),))
    reader = Toy(reader_task, bindings=(InputBinding(ledger_key="lane.note", field="note_ref"),))
    flow = Flow("flow", input=ChangeInput, outcomes=("done", "failed"))
    flow.step("writer", writer, on_failure="failed", then="reader")
    flow.step("reader", reader, on_failure="failed", then="done")
    _compile(flow, writer_task, reader_task)


def test_public_must_cover_outcomes_with_product_statuses() -> None:
    step = contract("step")
    flow = Flow("root", input=ChangeInput, outcomes=("done", "failed"), public={"done": "completed"})
    flow.step("step", step, on_failure="failed", then="done")
    with pytest.raises(FlowCheckError, match="public must cover"):
        _compile(flow, step)
    mapped = Flow(
        "root",
        input=ChangeInput,
        outcomes=("done", "failed"),
        public={"done": "completed", "failed": "failed"},
    )
    mapped.step("step", step, on_failure="failed", then="done")
    _compile(mapped, step)
