"""Control outputs are typed state fields. Parents read them by name."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from graph_engine.flow import Flow, FlowCheckError
from graph_engine.testing import committed

from support import CONFIG, RECEIPT, ChangeInput, Toy, contract, open_harness


class _Plan(BaseModel):
    plan_digest: str


class _Produced(BaseModel):
    plan: _Plan
    note: str = "n"


class _Child(BaseModel):
    plan_digest: str


class _Wider(BaseModel):
    plan_digest: int


def test_a_control_output_must_match_the_receiving_field() -> None:
    producer = contract("write", output_model=_Produced)
    child = Flow("child", input=_Child, outcomes=("ok", "failed"))
    child.step("read", contract("read", input_model=_Child), on_failure="failed", then="ok")
    parent = Flow("parent", input=ChangeInput, outcomes=("done", "failed"))
    parent.step("write", producer, on_failure="failed", then="lane")
    parent.control("write", plan_digest="plan.plan_digest")
    parent.subflow("lane", child, routes={"ok": "done", "failed": "failed"})
    _compile(parent, producer, contract("read", input_model=_Child))

    mismatched = Flow("child", input=_Wider, outcomes=("ok", "failed"))
    mismatched.step("read", contract("wide", input_model=_Wider), on_failure="failed", then="ok")
    bad = Flow("parent", input=ChangeInput, outcomes=("done", "failed"))
    bad.step("write", producer, on_failure="failed", then="lane")
    bad.control("write", plan_digest="plan.plan_digest")
    bad.subflow("lane", mismatched, routes={"ok": "done", "failed": "failed"})
    with pytest.raises(FlowCheckError, match="type does not match"):
        _compile(bad, producer, contract("wide", input_model=_Wider))


def test_a_parent_reads_a_control_output_by_name() -> None:
    producer = contract("write", output_model=_Produced)
    child = Flow("child", input=_Child, outcomes=("ok", "failed"))
    child.step("read", contract("read", input_model=_Child), on_failure="failed", then="ok")
    parent = Flow("parent", input=ChangeInput, outcomes=("done", "failed"))
    parent.step("write", producer, on_failure="failed", then="lane")
    parent.control("write", plan_digest="plan.plan_digest")
    parent.subflow(
        "lane",
        child,
        routes={"ok": "done", "failed": "failed"},
        inputs={"plan_digest": "plan.plan_digest"},
    )
    with pytest.raises(FlowCheckError, match="reads control plan_digest by name"):
        _compile(parent, producer, contract("read", input_model=_Child))


class _Reviewed(BaseModel):
    preparation_refs: tuple[str, ...]


class _ReviewOut(BaseModel):
    reviewed_case: _Reviewed | None = None
    note: str | None = None


def test_control_outputs_cannot_reuse_a_name_across_the_surface() -> None:
    producer = contract("write", output_model=_Produced)
    left = Flow("left", input=ChangeInput, outcomes=("ok", "failed"))
    left.step("write", producer, on_failure="failed", then="ok")
    left.control("write", plan_digest="plan.plan_digest")
    right = Flow("right", input=ChangeInput, outcomes=("ok", "failed"))
    right.step("write", producer, on_failure="failed", then="ok")
    right.control("write", plan_digest="plan.plan_digest")
    parent = Flow("parent", input=ChangeInput, outcomes=("done", "failed"))
    parent.subflow("left", left, routes={"ok": "right", "failed": "failed"})
    parent.subflow("right", right, routes={"ok": "done", "failed": "failed"})
    with pytest.raises(FlowCheckError, match="control plan_digest is declared twice"):
        _compile(parent, producer)


async def test_a_control_output_omits_a_none_object_and_writes_when_present() -> None:
    producer = contract("review", output_model=_ReviewOut)
    flow = Flow("review", input=ChangeInput, outcomes=("done", "failed"))
    flow.step("review", Toy(producer), on_failure="failed", then="done")
    flow.control("review", reviewed_refs="reviewed_case.preparation_refs", note="note")

    omitted_harness, omitted_context = open_harness(producer)
    omitted = flow.compile(omitted_context)
    omitted_harness._kernel.load_script(
        {"lane.review": [committed(_ReviewOut(reviewed_case=None, note=None), RECEIPT)]}
    )
    revised = await omitted.ainvoke({"change_id": "c1"}, config=CONFIG)
    assert "reviewed_refs" not in revised
    assert revised["note"] is None

    written_harness, written_context = open_harness(producer)
    written = flow.compile(written_context)
    written_harness._kernel.load_script(
        {
            "lane.review": [
                committed(
                    _ReviewOut(reviewed_case=_Reviewed(preparation_refs=("r1",)), note="kept"),
                    RECEIPT,
                )
            ]
        }
    )
    reviewed = await written.ainvoke({"change_id": "c1"}, config=CONFIG)
    assert reviewed["reviewed_refs"] == ["r1"]
    assert reviewed["note"] == "kept"


async def test_a_control_output_is_copied_onto_the_parent_state() -> None:
    producer = contract("write", output_model=_Produced)
    child_task = contract("read", input_model=_Child)
    child = Flow("child", input=_Child, outcomes=("ok", "failed"))
    child.step("read", child_task, on_failure="failed", then="ok")
    parent = Flow("parent", input=ChangeInput, outcomes=("done", "failed"))
    parent.step("write", Toy(producer), on_failure="failed", then="lane")
    parent.control("write", plan_digest="plan.plan_digest")
    parent.subflow("lane", child, routes={"ok": "done", "failed": "failed"})
    harness, context = open_harness(producer, child_task)
    graph = parent.compile(context)
    harness._kernel.load_script(
        {
            "lane.write": [committed(_Produced(plan=_Plan(plan_digest="ab")), RECEIPT)],
            "lane.read": [committed({"marker": "ok"}, RECEIPT)],
        }
    )
    result = await graph.ainvoke({"change_id": "c1"}, config=CONFIG)
    assert result["plan_digest"] == "ab"
    assert context.select_values[1]["plan_digest"] == "ab"


def _compile(flow: Flow, *tasks: object) -> None:
    _harness, context = open_harness(*tasks)  # type: ignore[arg-type]
    flow.compile(context)
