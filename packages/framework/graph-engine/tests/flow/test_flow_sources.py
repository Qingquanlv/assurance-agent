"""Ledger input sources and the generic entry write-back."""

from __future__ import annotations

import pytest
from pydantic import BaseModel, model_validator

from graph_engine.flow import Flow, FlowCheckError, ledger, ledger_receipt
from graph_engine.stategraph.ledger import NamedWrite
from graph_engine.testing import committed

from support import RECEIPT, ChangeInput, MarkerOutput, Toy, contract, open_harness, state_of


class _History:
    ledger_key = "lane.history"


class _ChildInput(BaseModel):
    change_id: str = "c1"
    history_refs: list[dict[str, str]] = []


class _Noted(BaseModel):
    change_id: str = "c1"
    note: str

    @model_validator(mode="before")
    @classmethod
    def _fill(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        if "note" not in data:
            data["note"] = "filled"
        elif data["note"] == "raw":
            data["note"] = "cooked"
        return data


def _child() -> Flow:
    flow = Flow("child", input=_ChildInput, outcomes=("done", "failed"))
    flow.step("read", contract("read", input_model=_ChildInput), on_failure="failed", then="done")
    return flow


def test_a_ledger_source_is_rejected_until_the_key_is_available() -> None:
    parent = Flow("parent", input=ChangeInput, outcomes=("done", "failed"))
    parent.subflow(
        "child",
        _child(),
        routes={"done": "done", "failed": "failed"},
        inputs={"history_refs": ledger(_History(), many=True)},
    )
    with pytest.raises(FlowCheckError, match="ledger key lane.history is not available at child"):
        parent.compile(open_harness(contract("read"))[1])

    declared = Flow(
        "parent",
        input=ChangeInput,
        outcomes=("done", "failed"),
        ledger_inputs=(_History(),),
    )
    declared.subflow(
        "child",
        _child(),
        routes={"done": "done", "failed": "failed"},
        inputs={"history_refs": ledger(_History(), many=True)},
    )
    declared.compile(open_harness(contract("read"))[1])

    writer = Toy(contract("write"), writes=(NamedWrite(name="history", root="qa/history", many=True),))
    written = Flow("parent", input=ChangeInput, outcomes=("done", "failed"))
    written.step("write", writer, on_failure="failed", then="child")
    written.subflow(
        "child",
        _child(),
        routes={"done": "done", "failed": "failed"},
        inputs={"history_refs": ledger(_History(), many=True)},
    )
    written.compile(open_harness(contract("write"), contract("read"))[1])


async def test_ledger_many_reads_the_declared_refs_into_the_child() -> None:
    parent = Flow(
        "parent",
        input=ChangeInput,
        outcomes=("done", "failed"),
        ledger_inputs=(_History(),),
    )
    parent.subflow(
        "child",
        _child(),
        routes={"done": "done", "failed": "failed"},
        inputs={"history_refs": ledger(_History(), many=True)},
    )
    task = contract("read", input_model=_ChildInput)
    harness, context = open_harness(task)
    refs = [{"path": "qa/history/1.json", "digest": "a" * 64}]
    result = await harness.run(
        parent.compile(context),
        input={"change_id": "c1", "artifact_ledger": {"lane.history": refs}},
        script={"lane.read": [committed(MarkerOutput(), RECEIPT)]},
    )
    assert state_of(result)["flow_outcome"] == "done"
    assert result.select_values[0]["history_refs"] == refs


async def test_entry_writes_back_filled_and_changed_fields_only() -> None:
    step = contract("step", input_model=_Noted)
    flow = Flow("lane", input=_Noted, outcomes=("done", "failed"))
    flow.step("step", step, on_failure="failed", then="done")
    harness, context = open_harness(step)
    graph = flow.compile(context)
    filled = await harness.run(
        graph,
        input={"change_id": "c1"},
        script={"lane.step": [committed(MarkerOutput(), RECEIPT)]},
    )
    assert filled.select_values[0]["note"] == "filled"

    kept = await harness.run(
        graph,
        input={"change_id": "c1", "note": "given"},
        script={"lane.step": [committed(MarkerOutput(), RECEIPT)]},
    )
    assert kept.select_values[0]["note"] == "given"

    cooked = await harness.run(
        graph,
        input={"change_id": "c1", "note": "raw"},
        script={"lane.step": [committed(MarkerOutput(), RECEIPT)]},
    )
    assert cooked.select_values[0]["note"] == "cooked"


class _NoteKey:
    ledger_key = "lane.note"


class _OptionalNote(BaseModel):
    change_id: str = "c1"
    note_ref: dict[str, str] | None = None


class _RequiredNote(BaseModel):
    change_id: str = "c1"
    note_ref: dict[str, str]


def _note_child(model: type[BaseModel]) -> Flow:
    flow = Flow("child", input=model, outcomes=("done", "failed"))
    flow.step("read", contract("read", input_model=model), on_failure="failed", then="done")
    return flow


def _note_parent(model: type[BaseModel]) -> Flow:
    parent = Flow(
        "parent",
        input=ChangeInput,
        outcomes=("done", "failed"),
        ledger_inputs=(_NoteKey(),),
    )
    parent.subflow(
        "child",
        _note_child(model),
        routes={"done": "done", "failed": "failed"},
        inputs={"note_ref": ledger(_NoteKey(), many=False)},
    )
    return parent


async def test_optional_ledger_passes_none_when_the_key_is_empty() -> None:
    task = contract("read", input_model=_OptionalNote)
    harness, context = open_harness(task)
    parent = _note_parent(_OptionalNote)
    empty = await harness.run(
        parent.compile(context),
        input={"change_id": "c1"},
        script={"lane.read": [committed(MarkerOutput(), RECEIPT)]},
    )
    assert empty.select_values[0]["note_ref"] is None

    ref = {"path": "qa/note.json", "digest": "a" * 64}
    present = await harness.run(
        parent.compile(context),
        input={"change_id": "c1", "artifact_ledger": {"lane.note": [ref]}},
        script={"lane.read": [committed(MarkerOutput(), RECEIPT)]},
    )
    assert present.select_values[0]["note_ref"] == ref

    with pytest.raises(ValueError, match="must hold one ref"):
        await harness.run(
            parent.compile(context),
            input={
                "change_id": "c1",
                "artifact_ledger": {"lane.note": [ref, ref | {"path": "qa/other.json"}]},
            },
            script={"lane.read": [committed(MarkerOutput(), RECEIPT)]},
        )


class _ReceiptNote(BaseModel):
    change_id: str = "c1"
    note_receipt: dict[str, str] | None = None


class _RequiredReceipt(BaseModel):
    change_id: str = "c1"
    note_receipt: dict[str, str]


def _receipt_parent(model: type[BaseModel]) -> Flow:
    parent = Flow(
        "parent",
        input=ChangeInput,
        outcomes=("done", "failed"),
        ledger_inputs=(_NoteKey(),),
    )
    parent.subflow(
        "child",
        _note_child(model),
        routes={"done": "done", "failed": "failed"},
        inputs={"note_receipt": ledger_receipt(_NoteKey())},
    )
    return parent


async def test_ledger_receipt_follows_the_latest_write_and_an_empty_optional_key() -> None:
    task = contract("read", input_model=_ReceiptNote)
    harness, context = open_harness(task)
    parent = _receipt_parent(_ReceiptNote)
    empty = await harness.run(
        parent.compile(context),
        input={"change_id": "c1"},
        script={"lane.read": [committed(MarkerOutput(), RECEIPT)]},
    )
    assert empty.select_values[0]["note_receipt"] is None

    ref = {"path": "qa/note.json", "digest": "a" * 64}
    present = await harness.run(
        parent.compile(context),
        input={
            "change_id": "c1",
            "artifact_ledger": {
                "lane.note": {
                    "refs": [ref],
                    "receipt": {"receipt_id": "receipt-9", "receipt_digest": "d" * 64},
                }
            },
        },
        script={"lane.read": [committed(MarkerOutput(), RECEIPT)]},
    )
    assert present.select_values[0]["note_receipt"] == {
        "receipt_id": "receipt-9",
        "receipt_digest": "d" * 64,
    }


async def test_required_ledger_receipt_rejects_an_empty_key() -> None:
    task = contract("read", input_model=_RequiredReceipt)
    harness, context = open_harness(task)
    parent = _receipt_parent(_RequiredReceipt)
    with pytest.raises(ValueError, match="must hold one receipt"):
        await harness.run(
            parent.compile(context),
            input={"change_id": "c1"},
            script={"lane.read": [committed(MarkerOutput(), RECEIPT)]},
        )
    with pytest.raises(FlowCheckError, match="ledger key lane.note is not available"):
        bare = Flow("parent", input=ChangeInput, outcomes=("done", "failed"))
        bare.subflow(
            "child",
            _note_child(_RequiredReceipt),
            routes={"done": "done", "failed": "failed"},
            inputs={"note_receipt": ledger_receipt(_NoteKey())},
        )
        bare.compile(open_harness(task)[1])


async def test_required_ledger_still_rejects_an_empty_key() -> None:
    task = contract("read", input_model=_RequiredNote)
    harness, context = open_harness(task)
    parent = _note_parent(_RequiredNote)
    with pytest.raises(ValueError, match="must hold one ref"):
        await harness.run(
            parent.compile(context),
            input={"change_id": "c1"},
            script={"lane.read": [committed(MarkerOutput(), RECEIPT)]},
        )
