from __future__ import annotations

from pydantic import BaseModel

from graph_engine.artifacts import ArtifactRef
from graph_engine.attempts.resolutions import PermanentTaskFailure
from graph_engine.flow import Flow
from graph_engine.stategraph.ledger import InputBinding, NamedWrite, ledger_refs
from graph_engine.testing import committed

from support import RECEIPT, ChangeInput, MarkerOutput, Toy, calls_of, contract, open_harness, state_of


class _ReadInput(BaseModel):
    change_id: str = "c1"
    note_ref: dict[str, str]


def _line():
    first = contract("first")
    second = contract("second", input_model=_ReadInput)
    third = contract("third")
    writer = Toy(first, writes=(NamedWrite(name="note", root="qa/note.md"),))
    reader = Toy(second, bindings=(InputBinding(ledger_key="lane.note", field="note_ref"),))
    flow = Flow("line", input=ChangeInput, outcomes=("done", "failed"))
    flow.step("first", writer, on_failure="failed", then="second")
    flow.step("second", reader, on_failure="failed", then="third")
    flow.step("third", third, on_failure="failed", then="done")
    harness, context = open_harness(first, second, third)
    return harness, flow.compile(context)


async def test_three_steps_pass_the_ledger_and_keep_semantic_ids() -> None:
    harness, graph = _line()
    note = ArtifactRef(path="qa/note.md", digest="a" * 64)
    result = await harness.run(
        graph,
        input={"change_id": "c1"},
        script={
            "lane.first": [committed(MarkerOutput(), RECEIPT, artifacts=(note,))],
            "lane.second": [committed(MarkerOutput(), RECEIPT)],
            "lane.third": [committed(MarkerOutput(), RECEIPT)],
        },
    )

    finished = state_of(result)
    assert finished["flow_outcome"] == "done"
    assert finished["flow_control"]["terminal"] == "done"
    assert calls_of(result) == ["lane.first", "lane.second", "lane.third"]
    assert result.select_values[1]["note_ref"] == {"path": "qa/note.md", "digest": "a" * 64}
    assert ledger_refs(finished["artifact_ledger"], "lane.note")[0]["digest"] == "a" * 64
    assert finished["artifact_ledger"]["lane.note"]["receipt"] == {
        "receipt_id": RECEIPT.receipt_id,
        "receipt_digest": RECEIPT.receipt_digest,
    }


async def test_a_middle_failure_stops_at_failed_and_skips_the_rest() -> None:
    harness, graph = _line()
    result = await harness.run(
        graph,
        input={"change_id": "c1"},
        script={
            "lane.first": [
                committed(
                    MarkerOutput(), RECEIPT, artifacts=(ArtifactRef(path="qa/note.md", digest="a" * 64),)
                )
            ],
            "lane.second": [PermanentTaskFailure(kind="invalid_output", message="bad")],
            "lane.third": [committed(MarkerOutput(), RECEIPT)],
        },
    )

    assert state_of(result)["flow_outcome"] == "failed"
    assert calls_of(result) == ["lane.first", "lane.second"]


async def test_a_recovered_failure_does_not_reroute_the_next_success() -> None:
    first = contract("first")
    second = contract("second")
    third = contract("third")
    flow = Flow("localize", input=ChangeInput, outcomes=("done", "failed"))
    flow.step("first", first, on_failure="second", then="third")
    flow.step("second", second, on_failure="failed", then="third")
    flow.step("third", third, on_failure="failed", then="done")
    harness, context = open_harness(first, second, third)
    graph = flow.compile(context)

    result = await harness.run(
        graph,
        input={"change_id": "c1"},
        script={
            "lane.first": [PermanentTaskFailure(kind="invalid_output", message="bad")],
            "lane.second": [committed(MarkerOutput(), RECEIPT)],
            "lane.third": [committed(MarkerOutput(), RECEIPT)],
        },
    )

    finished = state_of(result)
    assert finished["flow_outcome"] == "done"
    assert calls_of(result) == ["lane.first", "lane.second", "lane.third"]
    assert finished["attempt_failure"]["kind"] == "invalid_output"


async def test_failure_kinds_prefer_a_specific_permanent_key() -> None:
    step = contract("step")
    flow = Flow("kinds", input=ChangeInput, outcomes=("bad_output", "failed", "done"))
    flow.step(
        "step",
        step,
        on_failure={"permanent:invalid_output": "bad_output", "*": "failed"},
        then="done",
    )
    harness, context = open_harness(step)
    graph = flow.compile(context)

    invalid = await harness.run(
        graph,
        input={},
        script={"lane.step": [PermanentTaskFailure(kind="invalid_output", message="bad")]},
    )
    other = await harness.run(
        graph,
        input={},
        script={"lane.step": [PermanentTaskFailure(kind="invalid_input", message="bad")]},
    )

    assert state_of(invalid)["flow_outcome"] == "bad_output"
    assert state_of(other)["flow_outcome"] == "failed"
