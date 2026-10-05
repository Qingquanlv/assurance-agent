"""Bound flows keep their capability context when a product root mounts them."""

from __future__ import annotations

from typing import Any, Literal

import pytest
from langgraph.checkpoint.memory import MemorySaver
from pydantic import BaseModel

from graph_engine.attempts.resolutions import ReceiptRef
from graph_engine.flow import BoundFlow, Flow, FlowCheckError
from graph_engine.testing import committed

from support import CONFIG, RECEIPT, ChangeInput, MarkerOutput, Toy, contract, open_harness


class _Product:
    product_context = True

    def compile_root(self, builder: object) -> object:
        return builder.compile(checkpointer=None)  # type: ignore[attr-defined]

    def compile_subgraph(self, builder: object) -> object:
        return builder.compile(checkpointer=None)  # type: ignore[attr-defined]


class _Approval(BaseModel):
    action: Literal["approve", "reject"]


class _NeedsDigest(BaseModel):
    plan_digest: str


class _Digest(BaseModel):
    plan_digest: str = "d" * 64


class _DecisionInput(BaseModel):
    change_id: str = "c1"
    decision: str = "pass"


class _DecisionOutput(BaseModel):
    decision: str = "reject"


def _public() -> dict[str, str]:
    return {"completed": "completed", "failed": "failed"}


def test_a_product_root_rejects_a_step() -> None:
    task = contract("run")
    root = Flow("init", input=ChangeInput, outcomes=("completed", "failed"), public=_public())
    root.step("run", task, on_failure="failed", then="completed")
    with pytest.raises(FlowCheckError, match="only allows subflow nodes; run is a step"):
        root.compile(_Product())


def test_an_upstream_control_resolves_a_later_required_input() -> None:
    produce = contract("produce", output_model=_Digest)
    consume = contract("consume", input_model=_NeedsDigest)
    producer = Flow("prepare", input=ChangeInput, outcomes=("prepared", "failed"))
    producer.step("resolve", produce, on_failure="failed", then="prepared")
    producer.control("resolve", plan_digest="plan_digest")
    consumer = Flow("case", input=_NeedsDigest, outcomes=("ok", "failed"))
    consumer.step("read", consume, on_failure="failed", then="ok")
    parent = Flow("intake", input=ChangeInput, outcomes=("done", "failed"))
    parent.subflow("prepare", producer, routes={"prepared": "case", "failed": "failed"})
    parent.subflow("case", consumer, routes={"ok": "done", "failed": "failed"})
    _harness, context = open_harness(produce, consume)
    parent.compile(context)

    bare = Flow("intake", input=ChangeInput, outcomes=("done", "failed"))
    bare.subflow("case", consumer, routes={"ok": "done", "failed": "failed"})
    with pytest.raises(FlowCheckError, match="case input plan_digest cannot be resolved"):
        bare.compile(context)


async def test_bound_child_keeps_its_semantic_id_and_takes_the_root_interrupt_owner() -> None:
    review = contract("case-review")
    child = Flow("case", input=ChangeInput, outcomes=("ok", "failed"))
    child.step("case-review", Toy(review, namespace="intake"), on_failure="failed", then="human-review")
    child.gate(
        "human-review",
        decision=_Approval,
        routes={"approve": "ok", "reject": "failed"},
    )
    harness, context = open_harness(review)
    bound = child.bind(context)
    assert isinstance(bound, BoundFlow)
    root = Flow("intake", input=ChangeInput, outcomes=("completed", "failed"), public=_public())
    root.subflow("case", bound, routes={"ok": "completed", "failed": "failed"})
    graph = root.compile(_Product())
    graph.checkpointer = MemorySaver()
    harness._kernel.load_script({"intake.case-review": [committed(MarkerOutput(), RECEIPT)]})

    paused = await graph.ainvoke({"change_id": "c1"}, config=CONFIG)
    assert paused["__interrupt__"][0].value["interrupt_id"] == "intake.human-review"
    assert [call.semantic_node_id for call in harness._kernel.semantic_calls] == ["intake.case-review"]

    alone = child.compile(context)
    alone.checkpointer = MemorySaver()
    harness._kernel.load_script({"intake.case-review": [committed(MarkerOutput(), RECEIPT)]})
    paused_alone = await alone.ainvoke(
        {"change_id": "c1"},
        config={
            **CONFIG,
            "configurable": {**CONFIG["configurable"], "thread_id": "inv-2"},
        },
    )
    assert paused_alone["__interrupt__"][0].value["interrupt_id"] == "case.human-review"


def test_a_child_control_cannot_share_a_parent_input_name() -> None:
    review = contract("review", output_model=_DecisionOutput)
    child = Flow("review", input=ChangeInput, outcomes=("done", "failed"))
    child.step("review", Toy(review, namespace="improvement"), on_failure="failed", then="done")
    child.control("review", decision="decision")
    _harness, context = open_harness(review)
    root = Flow(
        "improvement-review",
        input=_DecisionInput,
        outcomes=("completed", "failed"),
        public=_public(),
    )
    root.subflow("feature", child.bind(context), routes={"done": "completed", "failed": "failed"})

    with pytest.raises(FlowCheckError, match="decision"):
        root.compile(_Product())


async def test_public_root_collects_nested_receipts_in_receipt_id_order() -> None:
    first = contract("first")
    second = contract("second")
    inner = Flow("archive", input=ChangeInput, outcomes=("done", "failed"))
    inner.step("first", Toy(first, namespace="improvement"), on_failure="failed", then="second")
    inner.step("second", Toy(second, namespace="improvement"), on_failure="failed", then="done")
    inner.publish_receipt("first")
    inner.publish_receipt("second")
    harness, context = open_harness(first, second)
    outer = Flow("improvement", input=ChangeInput, outcomes=("done", "failed"))
    outer.subflow("archive", inner, routes={"done": "done", "failed": "failed"})
    root = Flow("archive-root", input=ChangeInput, outcomes=("completed", "failed"), public=_public())
    root.subflow("improvement", outer.bind(context), routes={"done": "completed", "failed": "failed"})
    graph = root.compile(_Product())
    later = ReceiptRef(receipt_id="receipt-z", receipt_digest="a" * 64)
    earlier = ReceiptRef(receipt_id="receipt-a", receipt_digest="b" * 64)
    harness._kernel.load_script(
        {
            "improvement.first": [committed(MarkerOutput(), later)],
            "improvement.second": [committed(MarkerOutput(), earlier)],
        }
    )

    finished = await graph.ainvoke({"change_id": "c1"}, config=CONFIG)
    assert finished["status"] == "completed"
    assert finished["terminal"] == {"status": "completed", "reason": "completed"}
    assert finished["receipts"] == [
        {"receipt_id": "receipt-a", "receipt_digest": "b" * 64},
        {"receipt_id": "receipt-z", "receipt_digest": "a" * 64},
    ]
    assert finished["output"] == {
        "change_id": "c1",
        "status": "completed",
        "receipts": finished["receipts"],
    }


def _receipt(receipt_id: str, digest_char: str) -> ReceiptRef:
    return ReceiptRef(receipt_id=receipt_id, receipt_digest=digest_char * 64)


def _leaf(name: str, task: Any) -> Flow:
    flow = Flow(name, input=ChangeInput, outcomes=("done", "failed"))
    flow.step(name, Toy(task, namespace="lane"), on_failure="failed", then="done")
    flow.publish_receipt(name)
    return flow


async def test_nested_public_receipts_are_not_duplicated_across_subflows_or_loop_reentry() -> None:
    mark = contract("mark")
    first = contract("first")
    second = contract("second")
    pair = Flow("pair", input=ChangeInput, outcomes=("done", "failed"))
    pair.subflow("first", _leaf("first", first), routes={"done": "second", "failed": "failed"})
    pair.subflow("second", _leaf("second", second), routes={"done": "done", "failed": "failed"})
    root = Flow("root", input=ChangeInput, outcomes=("exhausted", "failed"))
    root.step("mark", Toy(mark, namespace="lane"), on_failure="failed", then="pair")
    root.publish_receipt("mark")
    with root.loop("cover", budget=1, on_exhausted="exhausted") as cover:
        root.subflow("pair", pair, routes={"done": cover.next("pair"), "failed": "failed"})
    harness, context = open_harness(mark, first, second)
    graph = root.compile(context)
    graph.checkpointer = MemorySaver()
    own = _receipt("receipt-root", "d")
    first_pass = (_receipt("receipt-a1", "a"), _receipt("receipt-b1", "b"))
    second_pass = (_receipt("receipt-a2", "c"), _receipt("receipt-b2", "e"))
    harness._kernel.load_script(
        {
            "lane.mark": [committed(MarkerOutput(), own)],
            "lane.first": [
                committed(MarkerOutput(), first_pass[0]),
                committed(MarkerOutput(), second_pass[0]),
            ],
            "lane.second": [
                committed(MarkerOutput(), first_pass[1]),
                committed(MarkerOutput(), second_pass[1]),
            ],
        }
    )

    finished = await graph.ainvoke({"change_id": "c1"}, config=CONFIG)

    expected = [
        {"receipt_id": item.receipt_id, "receipt_digest": item.receipt_digest}
        for item in (own, *first_pass, *second_pass)
    ]
    assert finished["flow_control"]["public_receipts"] == expected
    snapshots: list[list[dict[str, str]]] = []
    for snapshot in graph.get_state_history(CONFIG):
        control = snapshot.values.get("flow_control")
        if not isinstance(control, dict):
            continue
        receipts = control.get("public_receipts")
        if not isinstance(receipts, list) or not receipts:
            continue
        snapshots.append(receipts)
    assert snapshots
    for receipts in snapshots:
        assert receipts == expected[: len(receipts)]
        assert len(receipts) == len({item["receipt_id"] for item in receipts})


class _RecordingProduct:
    product_context = True

    def __init__(self) -> None:
        self.saver = MemorySaver()
        self.root_savers: list[object] = []
        self.child_savers: list[object] = []

    def compile_root(self, builder: object) -> object:
        compiled = builder.compile(checkpointer=self.saver)  # type: ignore[attr-defined]
        self.root_savers.append(compiled.checkpointer)
        return compiled

    def compile_subgraph(self, builder: object) -> object:
        compiled = builder.compile(checkpointer=None)  # type: ignore[attr-defined]
        self.child_savers.append(compiled.checkpointer)
        return compiled


def _nested_product(task: object, capability: object) -> Flow:
    work = Flow("work", input=ChangeInput, outcomes=("done", "failed"))
    work.step("run", Toy(task, namespace="lane"), on_failure="failed", then="done")  # type: ignore[arg-type]
    inner = Flow("inner", input=ChangeInput, outcomes=("done", "failed"))
    inner.subflow(
        "work",
        work.bind(capability),  # type: ignore[arg-type]
        routes={"done": "done", "failed": "failed"},
    )
    root = Flow("root", input=ChangeInput, outcomes=("done", "failed"))
    root.subflow("inner", inner, routes={"done": "done", "failed": "failed"})
    return root


async def test_a_nested_product_flow_runs_and_only_the_root_gets_the_checkpointer() -> None:
    task = contract("run")
    harness, capability = open_harness(task)
    root = _nested_product(task, capability)
    product = _RecordingProduct()
    graph = root.compile(product)
    assert product.root_savers == [product.saver]
    assert product.child_savers == [None]
    assert graph.checkpointer is product.saver
    harness._kernel.load_script({"lane.run": [committed(MarkerOutput(), RECEIPT)]})

    finished = await graph.ainvoke({"change_id": "c1"}, config=CONFIG)

    assert finished["flow_outcome"] == "done"


def test_a_step_inside_a_nested_product_flow_is_rejected() -> None:
    task = contract("run")
    inner = Flow("inner", input=ChangeInput, outcomes=("done", "failed"))
    inner.step("run", Toy(task, namespace="lane"), on_failure="failed", then="done")
    root = Flow("root", input=ChangeInput, outcomes=("done", "failed"))
    root.subflow("inner", inner, routes={"done": "done", "failed": "failed"})
    with pytest.raises(FlowCheckError, match="only allows subflow nodes; run is a step"):
        root.compile(_Product())
