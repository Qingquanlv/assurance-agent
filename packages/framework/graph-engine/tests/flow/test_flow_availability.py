from typing import Literal

import pytest
from pydantic import BaseModel

from graph_engine.flow import Flow, FlowCheckError, ledger
from graph_engine.flow.check import check_flow
from graph_engine.stategraph.ledger import InputBinding, LedgerArtifact, NamedWrite
from support import ChangeInput, Toy, contract


class Needs(BaseModel):
    note_ref: dict[str, str]


class Choice(BaseModel):
    choice: Literal["write", "skip"]


def reader() -> Toy:
    return Toy(contract("read", input_model=Needs), bindings=(InputBinding("lane.note", "note_ref"),))


def writer() -> Toy:
    return Toy(contract("write"), writes=(NamedWrite("note", "note.json"),))


def test_explicit_required_ledger_requires_a_producer() -> None:
    flow = Flow("flow", input=ChangeInput, outcomes=("done", "failed"))
    flow.step(
        "read",
        Toy(contract("read", input_model=Needs)),
        inputs={"note_ref": ledger(LedgerArtifact("lane.note"), many=False)},
        then="done",
        on_failure="failed",
    )
    with pytest.raises(FlowCheckError, match="ledger key lane.note"):
        check_flow(flow)


@pytest.mark.parametrize("failure", [False, True])
def test_reader_requires_write_on_every_path(failure: bool) -> None:
    flow = Flow("flow", input=ChangeInput, outcomes=("done", "failed"))
    if not failure:
        flow.step(
            "choose",
            Toy(contract("choose", output_model=Choice)),
            route_on="choice",
            routes={"write": "write", "skip": "read"},
            on_failure="failed",
        )
    flow.step("write", writer(), then="read", on_failure="read" if failure else "failed")
    flow.step("read", reader(), then="done", on_failure="failed")
    with pytest.raises(FlowCheckError, match="ledger key lane.note"):
        check_flow(flow)


def test_mutually_exclusive_producers_are_guaranteed() -> None:
    flow = Flow("flow", input=ChangeInput, outcomes=("done", "failed"))
    flow.step(
        "choose",
        Toy(contract("choose", output_model=Choice)),
        route_on="choice",
        routes={"write": "left", "skip": "right"},
        on_failure="failed",
    )
    flow.step("left", writer(), then="read", on_failure="failed")
    flow.step("right", writer(), then="read", on_failure="failed")
    flow.step("read", reader(), then="done", on_failure="failed")
    check_flow(flow)


def test_controls_reject_business_dictionary() -> None:
    class Business(BaseModel):
        payload: dict[str, list[str]]

    flow = Flow("flow", input=ChangeInput, outcomes=("done", "failed"))
    flow.step("write", Toy(contract("write", output_model=Business)), then="done", on_failure="failed")
    flow.control("write", payload="payload")
    with pytest.raises(FlowCheckError, match="control payload"):
        check_flow(flow)


@pytest.mark.parametrize(
    ("failure", "category"),
    [
        ({"resolution_kind": "permanent", "kind": "invalid_output"}, "invalid_output"),
        ({"resolution_kind": "permanent", "kind": "invalid_input"}, "invalid_input"),
        ({"resolution_kind": "rejected", "reason": "policy"}, "rejected"),
        ({"resolution_kind": "permanent", "kind": "handler_exception"}, "failed"),
    ],
)
def test_stable_failure_categories(failure: dict[str, str], category: str) -> None:
    from graph_engine.flow.compile import _match_failure

    assert _match_failure({category: "handled", "*": "fallback"}, failure) == "handled"


def test_child_outcome_does_not_promise_possible_write() -> None:
    child = Flow("child", input=ChangeInput, outcomes=("done",))
    child.step("write", writer(), then="done", on_failure="done")
    parent = Flow("parent", input=ChangeInput, outcomes=("done", "failed"))
    parent.subflow("child", child, routes={"done": "read"})
    parent.step("read", reader(), then="done", on_failure="failed")
    with pytest.raises(FlowCheckError, match="ledger key lane.note"):
        check_flow(parent)


def test_selected_parallel_does_not_promise_branch_write() -> None:
    class Selected(ChangeInput):
        selected: list[str]

    flow = Flow("flow", input=Selected, outcomes=("done", "failed"))
    flow.parallel(
        "work",
        branches={"write": writer()},
        select="selected",
        require="succeeded",
        then="read",
        on_failure="failed",
    )
    flow.step("read", reader(), then="done", on_failure="failed")
    with pytest.raises(FlowCheckError, match="ledger key lane.note"):
        check_flow(flow)


@pytest.mark.parametrize("stale", [False, True])
def test_loop_requires_first_entry_but_preserves_seeded_carry(stale: bool) -> None:
    flow = Flow("flow", input=ChangeInput, outcomes=("done", "failed"))
    if stale:
        flow.step("seed", writer(), then="read", on_failure="failed")
    with flow.loop("rounds", budget=2, on_exhausted="done") as loop:
        flow.step("read", reader(), then="write", on_failure="failed")
        flow.step("write", writer(), then=loop.next("read"), on_failure="failed")
    if stale:
        check_flow(flow)
    else:
        with pytest.raises(FlowCheckError, match="ledger key lane.note"):
            check_flow(flow)


def test_optional_and_default_slots_do_not_require_a_writer() -> None:
    class OptionalNeeds(BaseModel):
        note_ref: dict[str, str] | None = None

    flow = Flow("flow", input=ChangeInput, outcomes=("done", "failed"))
    flow.step(
        "read",
        Toy(contract("read", input_model=OptionalNeeds), bindings=(InputBinding("lane.note", "note_ref"),)),
        then="done",
        on_failure="failed",
    )
    check_flow(flow)


def test_external_ledger_and_typed_control_exports_are_accepted() -> None:
    from graph_engine.artifacts import ArtifactRef

    class Evidence(ArtifactRef):
        purpose: str = "evidence"

    class Metadata(BaseModel):
        digest: str
        selected: list[Literal["unit", "integration"]]
        refs: tuple[Evidence, ...]

    flow = Flow(
        "flow", input=ChangeInput, outcomes=("done", "failed"), ledger_inputs=(LedgerArtifact("lane.note"),)
    )
    flow.step("read", reader(), then="export", on_failure="failed")
    flow.step("export", Toy(contract("export", output_model=Metadata)), then="done", on_failure="failed")
    flow.control("export", digest="digest", selected="selected", refs="refs")
    check_flow(flow)


def test_controls_reject_nested_business_models() -> None:
    class Business(BaseModel):
        count: int

    class Output(BaseModel):
        business: Business

    flow = Flow("flow", input=ChangeInput, outcomes=("done", "failed"))
    flow.step("write", Toy(contract("write", output_model=Output)), then="done", on_failure="failed")
    flow.control("write", business="business")
    with pytest.raises(FlowCheckError, match="control business"):
        check_flow(flow)


def test_same_name_input_can_supply_an_absent_slot() -> None:
    flow = Flow("flow", input=Needs, outcomes=("done", "failed"))
    flow.step("read", reader(), then="done", on_failure="failed")
    check_flow(flow)


def test_parallel_optional_slot_can_use_its_default() -> None:
    class OptionalNeeds(BaseModel):
        note_ref: dict[str, str] | None = None

    branch = Toy(
        contract("read", input_model=OptionalNeeds), bindings=(InputBinding("lane.note", "note_ref"),)
    )
    flow = Flow("flow", input=ChangeInput, outcomes=("done", "failed"))
    flow.parallel(
        "work", branches={"read": branch}, select=None, require="succeeded", then="done", on_failure="failed"
    )
    check_flow(flow)


def test_required_nullable_implicit_slot_still_requires_input() -> None:
    class NullableNeeds(BaseModel):
        note_ref: dict[str, str] | None

    flow = Flow("flow", input=ChangeInput, outcomes=("done", "failed"))
    flow.step(
        "read",
        Toy(contract("read", input_model=NullableNeeds), bindings=(InputBinding("lane.note", "note_ref"),)),
        then="done",
        on_failure="failed",
    )
    with pytest.raises(FlowCheckError, match="ledger key lane.note"):
        check_flow(flow)


def test_required_control_cannot_be_bypassed() -> None:
    class Metadata(BaseModel):
        digest: str

    child = Flow("child", input=Metadata, outcomes=("done", "failed"))
    child.step("read", Toy(contract("read", input_model=Metadata)), then="done", on_failure="failed")
    flow = Flow("flow", input=ChangeInput, outcomes=("done", "failed"))
    flow.step(
        "choose",
        Toy(contract("choose", output_model=Choice)),
        route_on="choice",
        routes={"write": "write", "skip": "child"},
        on_failure="failed",
    )
    flow.step("write", Toy(contract("write", output_model=Metadata)), then="child", on_failure="failed")
    flow.control("write", digest="digest")
    flow.subflow("child", child, routes={"done": "done", "failed": "failed"})
    with pytest.raises(FlowCheckError, match="digest"):
        check_flow(flow)


def test_gate_source_cannot_be_bypassed() -> None:
    class Decision(BaseModel):
        action: Literal["approve"]

    class NeedsAction(BaseModel):
        action: str

    flow = Flow("flow", input=ChangeInput, outcomes=("done", "failed"))
    flow.step(
        "choose",
        Toy(contract("choose", output_model=Choice)),
        route_on="choice",
        routes={"write": "gate", "skip": "read"},
        on_failure="failed",
    )
    gate = flow.gate("gate", decision=Decision, routes={"approve": "read"})
    flow.step(
        "read",
        Toy(contract("read", input_model=NeedsAction)),
        inputs={"action": gate.action},
        then="done",
        on_failure="failed",
    )
    with pytest.raises(FlowCheckError, match="gate"):
        check_flow(flow)


async def test_guaranteed_same_name_control_supplies_a_step_input() -> None:
    from graph_engine.testing import committed
    from support import RECEIPT, MarkerOutput, open_harness, state_of

    class Metadata(BaseModel):
        digest: str

    produce = contract("produce", output_model=Metadata)
    consume = contract("consume", input_model=Metadata)
    flow = Flow("flow", input=ChangeInput, outcomes=("done", "failed"))
    flow.step("produce", Toy(produce), then="consume", on_failure="failed")
    flow.control("produce", digest="digest")
    flow.step("consume", Toy(consume), then="done", on_failure="failed")
    harness, context = open_harness(produce, consume)
    result = await harness.run(
        flow.compile(context),
        input={"change_id": "c1"},
        script={
            "lane.produce": [committed(Metadata(digest="abc"), RECEIPT)],
            "lane.consume": [committed(MarkerOutput(), RECEIPT)],
        },
    )
    assert state_of(result)["flow_outcome"] == "done"


def test_dataflow_reports_recursive_mount_as_a_flow_error() -> None:
    flow = Flow("flow", input=ChangeInput, outcomes=("done", "failed"))
    flow.step("start", Toy(contract("start")), then="recursive", on_failure="failed")
    flow.subflow("recursive", flow, routes={"done": "done", "failed": "failed"})
    with pytest.raises(FlowCheckError, match="itself"):
        check_flow(flow)


@pytest.mark.parametrize("required", ["failed", "succeeded"])
def test_static_parallel_op_guarantees_writes_only_on_success(required: str) -> None:
    flow = Flow("flow", input=ChangeInput, outcomes=("done", "failed"))
    flow.parallel(
        "work",
        branches={"write": writer()},
        select=None,
        require=required,
        then="read",
        on_failure="failed",
    )
    flow.step("read", reader(), then="done", on_failure="failed")
    if required == "failed":
        with pytest.raises(FlowCheckError, match="ledger key lane.note"):
            check_flow(flow)
    else:
        check_flow(flow)


@pytest.mark.parametrize("mounted", [False, True])
@pytest.mark.parametrize("nullable_intermediate", [False, True])
def test_required_control_requires_a_non_omittable_export_path(
    mounted: bool, nullable_intermediate: bool
) -> None:
    from pydantic import create_model

    class Inner(BaseModel):
        value: str

    class Required(BaseModel):
        token: str

    output = create_model("Output", inner=(Inner | None if nullable_intermediate else Inner, ...))
    producer = Flow("producer", input=ChangeInput, outcomes=("done", "failed"))
    producer.step("write", Toy(contract("write", output_model=output)), then="done", on_failure="failed")
    producer.control("write", token="inner.value")
    if mounted:
        flow = Flow("parent", input=ChangeInput, outcomes=("done", "failed"))
        flow.subflow("producer", producer, routes={"done": "read", "failed": "failed"})
    else:
        flow = Flow("direct", input=ChangeInput, outcomes=("done", "failed"))
        flow.step("write", Toy(contract("write", output_model=output)), then="read", on_failure="failed")
        flow.control("write", token="inner.value")
    flow.step("read", Toy(contract("read", input_model=Required)), then="done", on_failure="failed")
    if nullable_intermediate:
        with pytest.raises(FlowCheckError, match="token"):
            check_flow(flow)
    else:
        check_flow(flow)


def test_optional_control_leaf_is_guaranteed_and_written_as_none() -> None:
    from graph_engine.flow.compile import _control_value
    from graph_engine.flow.dataflow import guaranteed

    class Inner(BaseModel):
        value: str | None

    class Output(BaseModel):
        inner: Inner

    class Required(BaseModel):
        token: str | None

    flow = Flow("flow", input=ChangeInput, outcomes=("done", "failed"))
    flow.step("write", Toy(contract("write", output_model=Output)), then="read", on_failure="failed")
    flow.control("write", token="inner.value")
    flow.step("read", Toy(contract("read", input_model=Required)), then="done", on_failure="failed")
    check_flow(flow)
    assert guaranteed(flow, "control")["read"] == {"token"}
    assert _control_value(Output(inner=Inner(value=None)).model_dump(), "inner.value") is None


@pytest.mark.parametrize("fallback", ["default", "optional", "parent"])
def test_omittable_control_preserves_supported_input_fallbacks(fallback: str) -> None:
    class Inner(BaseModel):
        value: str

    class Output(BaseModel):
        inner: Inner | None

    class DefaultConsumer(BaseModel):
        token: str = "fallback"

    class OptionalConsumer(BaseModel):
        token: str | None = None

    class RequiredConsumer(BaseModel):
        token: str

    class ParentInput(BaseModel):
        change_id: str
        parent_token: str

    consumer = {"default": DefaultConsumer, "optional": OptionalConsumer, "parent": RequiredConsumer}[
        fallback
    ]
    flow = Flow(
        "flow", input=ParentInput if fallback == "parent" else ChangeInput, outcomes=("done", "failed")
    )
    flow.step("write", Toy(contract("write", output_model=Output)), then="read", on_failure="failed")
    flow.control("write", token="inner.value")
    flow.step(
        "read",
        Toy(contract("read", input_model=consumer)),
        inputs={"token": "parent_token"} if fallback == "parent" else {},
        then="done",
        on_failure="failed",
    )
    check_flow(flow)
