from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

import pytest
from langgraph.graph import END, START, StateGraph

from assurance_intake.contracts.agent import ArtifactListResultV1
from assurance_intake.contracts.attempts import AGENT_JOB_CONTRACTS
from assurance_intake.graphs.factory import build_intake_graphs
from assurance_intake.graphs.nodes import (
    advance_join,
    apply_current_trigger,
    select_case_design_retry,
)
from assurance_intake.graphs.state import (
    CASE_REVIEW_PREDECESSORS,
    CaseReviewArrival,
    IntakeState,
    consume_case_review_trigger,
    empty_case_review_inbox,
    make_case_review_arrival,
    merge_case_review_inbox,
    offer_case_review_arrival,
)
from graph_engine.attempts.contracts import TaskAttemptContract
from graph_engine.attempts.resolutions import ReceiptRef
from graph_engine.testing import GraphHarness, committed

_PREDECESSORS = (
    "review-round-advance",
    "review-round-advance-retry",
    "review-round-advance-rework-retry",
)
_CURRENT_TRIGGER_ROWS = (("assurance.intake.workflow.graph.entry", "advance-join"),)


def _arrival(
    predecessor: str,
    *,
    epoch: int = 0,
    sequence: int = 1,
    used: int = 1,
    budget: int = 2,
) -> CaseReviewArrival:
    return make_case_review_arrival(
        predecessor=predecessor,
        business_epoch=epoch,
        sequence=sequence,
        value={"rounds_used": used, "rounds_budget": budget},
        source_activation=f"{predecessor}-{epoch}-{sequence}",
    )


def test_join_predecessors_are_the_three_advance_sites() -> None:
    assert CASE_REVIEW_PREDECESSORS == _PREDECESSORS
    assert set(CASE_REVIEW_PREDECESSORS) == set(_PREDECESSORS)


@pytest.mark.parametrize(
    "row",
    _CURRENT_TRIGGER_ROWS,
    ids=lambda row: f"{row[0]}/{row[1]}",
)
def test_current_trigger(row: tuple[str, str]) -> None:
    graph_id, anchor = row
    assert graph_id == "assurance.intake.workflow.graph.entry"
    assert anchor == "advance-join"
    arrival = _arrival("review-round-advance", used=1, budget=2)
    inbox = offer_case_review_arrival(empty_case_review_inbox(), arrival)
    applied = apply_current_trigger(
        {
            "change_id": "CH-DEMO-001",
            "selected_test_families": ["api"],
            "case_delta_paths": ["qa/changes/CH-DEMO-001/cases/menus/case.yaml"],
            "capability_leafs": ["entities.item.create"],
            "allowed_artifact_paths": ["qa/changes"],
            "rounds_used": 0,
            "rounds_budget": 2,
            "current_trigger": _arrival("review-round-advance-retry", used=9, budget=9),
            "predecessor_tokens": {"advance-join": {"tokens": [{"rounds_used": 9, "rounds_budget": 9}]}},
            "case_review_inbox": inbox,
        }
    )
    assert applied["current_trigger"] == arrival
    assert applied["rounds_used"] == 1
    assert applied["rounds_budget"] == 2
    assert applied["current_trigger"] != applied.get("predecessor_tokens")


def test_first_arrival_becomes_exact_current_trigger() -> None:
    arrival = _arrival("review-round-advance", used=1, budget=2)
    inbox = offer_case_review_arrival(empty_case_review_inbox(), arrival)
    current = inbox["current_trigger"]
    assert current == arrival
    assert inbox["arrivals"] == [arrival]
    assert inbox["dispatched_ids"] == []
    assert current is not None
    assert current["value"] == {"rounds_used": 1, "rounds_budget": 2}
    assert current["business_epoch"] == 0
    assert current["predecessor"] == "review-round-advance"
    assert current["arrival_id"] == arrival["arrival_id"]


def test_late_second_arrival_in_same_epoch_is_retained_and_dispatched_once() -> None:
    first = _arrival("review-round-advance", epoch=0, sequence=1, used=1)
    late = _arrival("review-round-advance-retry", epoch=0, sequence=2, used=1)
    inbox = offer_case_review_arrival(empty_case_review_inbox(), first)
    inbox = offer_case_review_arrival(inbox, late)
    assert inbox["current_trigger"] == first
    assert inbox["arrivals"] == [first, late]
    first_dispatch = consume_case_review_trigger(inbox)
    assert first_dispatch["current_trigger"] == late
    assert first["arrival_id"] in first_dispatch["dispatched_ids"]
    assert late["arrival_id"] not in first_dispatch["dispatched_ids"]
    second_dispatch = consume_case_review_trigger(first_dispatch)
    assert second_dispatch["current_trigger"] is None
    assert set(second_dispatch["dispatched_ids"]) == {first["arrival_id"], late["arrival_id"]}
    replay_late = offer_case_review_arrival(second_dispatch, late)
    assert replay_late["current_trigger"] is None
    assert replay_late["dispatched_ids"].count(late["arrival_id"]) == 1


def test_replay_of_the_same_arrival_id_is_deduplicated() -> None:
    arrival = _arrival("review-round-advance-rework-retry", sequence=4)
    inbox = offer_case_review_arrival(empty_case_review_inbox(), arrival)
    replayed = offer_case_review_arrival(inbox, arrival)
    assert replayed["arrivals"] == [arrival]
    assert replayed["current_trigger"] == arrival


def test_two_reducer_merge_orders_produce_identical_inbox_state() -> None:
    first = _arrival("review-round-advance", sequence=1, used=1)
    second = _arrival("review-round-advance-retry", sequence=2, used=1)
    empty = empty_case_review_inbox()
    left = merge_case_review_inbox(
        offer_case_review_arrival(empty, first),
        offer_case_review_arrival(empty, second),
    )
    right = merge_case_review_inbox(
        offer_case_review_arrival(empty, second),
        offer_case_review_arrival(empty, first),
    )
    assert left == right
    assert left["arrivals"] == [first, second]
    assert left["current_trigger"] == first
    assert isinstance(left, dict)


def test_repeated_business_epochs_preserve_exact_rounds_used_and_budget() -> None:
    epoch_one = _arrival("review-round-advance", epoch=0, sequence=1, used=1, budget=2)
    epoch_two = _arrival("review-round-advance-retry", epoch=1, sequence=1, used=2, budget=2)
    inbox = consume_case_review_trigger(offer_case_review_arrival(empty_case_review_inbox(), epoch_one))
    inbox = offer_case_review_arrival(inbox, epoch_two)
    current = inbox["current_trigger"]
    assert current == epoch_two
    assert current is not None
    assert current["value"] == {"rounds_used": 2, "rounds_budget": 2}
    assert current["value"] != {"rounds_used": 3, "rounds_budget": 2}
    assert current["business_epoch"] == 1


def test_dispatch_cursor_never_reclaims_a_consumed_arrival() -> None:
    arrival = _arrival("review-round-advance", sequence=1)
    inbox = consume_case_review_trigger(offer_case_review_arrival(empty_case_review_inbox(), arrival))
    assert inbox["current_trigger"] is None
    assert arrival["arrival_id"] in inbox["dispatched_ids"]
    reclaimed = offer_case_review_arrival(inbox, arrival)
    assert reclaimed["current_trigger"] is None
    assert reclaimed["dispatched_ids"] == [arrival["arrival_id"]]
    leftover = merge_case_review_inbox(reclaimed, empty_case_review_inbox())
    assert leftover["current_trigger"] is None


def test_downstream_case_design_retry_reads_current_trigger_value_only() -> None:
    stale_rounds = {"rounds_used": 0, "rounds_budget": 2}
    arrival = _arrival("review-round-advance-retry", used=1, budget=2)
    inbox = offer_case_review_arrival(empty_case_review_inbox(), arrival)
    state: dict[str, Any] = {
        "change_id": "CH-DEMO-001",
        "selected_test_families": ["api"],
        "case_delta_paths": ["qa/changes/CH-DEMO-001/cases/menus/case.yaml"],
        "capability_leafs": ["entities.item.create"],
        "allowed_artifact_paths": ["qa/changes"],
        **stale_rounds,
        "predecessor_tokens": {"advance-join": {"tokens": [{"rounds_used": 9, "rounds_budget": 9}]}},
        "case_review_inbox": inbox,
    }
    applied = apply_current_trigger(state)
    assert applied["rounds_used"] == 1
    assert applied["rounds_budget"] == 2
    assert applied["current_trigger"] == arrival
    selected = select_case_design_retry({**state, **applied})
    assert selected.change_id == "CH-DEMO-001"
    assert applied["rounds_used"] == arrival["value"]["rounds_used"]
    assert applied["rounds_used"] != state["rounds_used"]
    assert "tokens" not in applied
    trigger = applied["current_trigger"]
    assert isinstance(trigger, dict)
    assert trigger["value"] == arrival["value"]


_SHA = "a" * 64
_RECEIPT = ReceiptRef(receipt_id="receipt-1", receipt_digest=_SHA)


def _contracts() -> dict[str, TaskAttemptContract[Any, Any]]:
    return {contract.contract_id: contract.to_task_contract() for contract in AGENT_JOB_CONTRACTS.values()}


def _prepare_input() -> dict[str, object]:
    return {
        "change_id": "CH-DEMO-001",
        "requirement": "Cover department CRUD.",
        "selected_test_families": ["api"],
        "case_delta_paths": ["qa/changes/CH-DEMO-001/cases/menus/case.yaml"],
        "capability_leafs": ["entities.item.create"],
        "allowed_artifact_paths": ["qa/changes"],
        "rounds_used": 0,
        "rounds_budget": 2,
    }


def _artifact() -> ArtifactListResultV1:
    return ArtifactListResultV1(output_files=("qa/changes/CH-DEMO-001/proposal.md",))


def _design() -> dict[str, object]:
    return {
        "output_files": ["qa/changes/CH-DEMO-001/proposal.md"],
        "validation_status": "pass",
        "artifacts": [{"path": "qa/changes/CH-DEMO-001/proposal.md", "digest": _SHA}],
    }


def _review(
    decision: str,
    *,
    auto_fix: bool = False,
    used: int = 0,
    budget: int = 2,
) -> dict[str, object]:
    return {
        "decision": decision,
        "auto_fix_allowed": auto_fix,
        "human_review_required": False,
        "artifacts": [{"path": "qa/changes", "digest": _SHA}],
        "rounds_used": used,
        "rounds_budget": budget,
    }


def _compile_join_graph() -> Any:
    builder: StateGraph[IntakeState] = StateGraph(IntakeState)
    builder.add_node("advance-join", cast(Callable[..., Any], advance_join))
    builder.add_edge(START, "advance-join")
    builder.add_edge("advance-join", END)
    return builder.compile(checkpointer=None)


def _offer_node(arrival: CaseReviewArrival, *, poison_lww: bool) -> Callable[..., dict[str, object]]:
    def _offer(state: dict[str, Any]) -> dict[str, object]:
        inbox = offer_case_review_arrival(
            state.get("case_review_inbox") or empty_case_review_inbox(),
            arrival,
        )
        update: dict[str, object] = {"case_review_inbox": inbox}
        if poison_lww:
            update["current_trigger"] = arrival
        return update

    return _offer


def _compile_two_predecessor_join(*, late_writes_lww: bool, first_added_first: bool) -> Any:
    first = _arrival("review-round-advance", epoch=0, sequence=1, used=1)
    late = _arrival("review-round-advance-retry", epoch=0, sequence=1, used=1)
    nodes = [
        ("review-round-advance", _offer_node(first, poison_lww=not late_writes_lww)),
        ("review-round-advance-retry", _offer_node(late, poison_lww=late_writes_lww)),
    ]
    ordered = nodes if first_added_first else list(reversed(nodes))
    builder: StateGraph[IntakeState] = StateGraph(IntakeState)
    for name, node in ordered:
        builder.add_node(name, cast(Callable[..., Any], node))
    builder.add_node("advance-join", cast(Callable[..., Any], advance_join))
    builder.add_edge(START, "review-round-advance")
    builder.add_edge(START, "review-round-advance-retry")
    builder.add_edge("review-round-advance", "advance-join")
    builder.add_edge("review-round-advance-retry", "advance-join")
    builder.add_edge("advance-join", END)
    return builder.compile(checkpointer=None)


def _join_seed() -> dict[str, object]:
    return {
        "change_id": "CH-DEMO-001",
        "requirement": "Cover department CRUD.",
        "selected_test_families": ["api"],
        "case_delta_paths": ["qa/changes/CH-DEMO-001/cases/menus/case.yaml"],
        "capability_leafs": ["entities.item.create"],
        "allowed_artifact_paths": ["qa/changes"],
        "rounds_used": 0,
        "rounds_budget": 2,
    }


async def test_compiled_join_reads_inbox_cursor_not_lww_shadow() -> None:
    first = _arrival("review-round-advance", epoch=0, sequence=1, used=1)
    late = _arrival("review-round-advance-retry", epoch=0, sequence=2, used=9, budget=9)
    inbox = offer_case_review_arrival(empty_case_review_inbox(), first)
    inbox = offer_case_review_arrival(inbox, late)
    assert inbox["current_trigger"] == first
    result = await _compile_join_graph().ainvoke(
        {
            **_join_seed(),
            "current_trigger": late,
            "case_review_inbox": inbox,
            "rounds_used": 0,
            "rounds_budget": 2,
        }
    )
    assert result["current_trigger"] == first
    assert result["case_review_inbox"]["current_trigger"] == first
    assert result["case_review_inbox"]["arrivals"] == [first, late]
    assert result["rounds_used"] == 1
    assert result["rounds_budget"] == 2
    assert result["rounds_used"] == first["value"]["rounds_used"]
    assert result["current_trigger"] != late


async def test_compiled_graph_same_epoch_arrivals_retained_first_current_then_late_dispatched() -> None:
    first = _arrival("review-round-advance", epoch=0, sequence=1, used=1)
    late = _arrival("review-round-advance-retry", epoch=0, sequence=1, used=1)
    result = await _compile_two_predecessor_join(late_writes_lww=True, first_added_first=True).ainvoke(
        _join_seed()
    )
    inbox = result["case_review_inbox"]
    assert [item["arrival_id"] for item in inbox["arrivals"]] == [first["arrival_id"], late["arrival_id"]]
    assert inbox["current_trigger"] == first
    assert result["current_trigger"] == first
    assert result["rounds_used"] == first["value"]["rounds_used"]
    consumed = consume_case_review_trigger(inbox)
    assert consumed["current_trigger"] == late
    late_result = await _compile_join_graph().ainvoke(
        {
            **result,
            "current_trigger": first,
            "case_review_inbox": consumed,
        }
    )
    assert late_result["current_trigger"] == late
    assert late_result["case_review_inbox"]["current_trigger"] == late
    assert first["arrival_id"] in late_result["case_review_inbox"]["dispatched_ids"]
    assert late["arrival_id"] not in late_result["case_review_inbox"]["dispatched_ids"]
    finished = consume_case_review_trigger(late_result["case_review_inbox"])
    assert finished["current_trigger"] is None
    assert set(finished["dispatched_ids"]) == {first["arrival_id"], late["arrival_id"]}
    replay = offer_case_review_arrival(finished, late)
    assert replay["current_trigger"] is None
    assert replay["dispatched_ids"].count(late["arrival_id"]) == 1


async def test_compiled_graph_two_reducer_merge_orders_are_identical() -> None:
    late_last = await _compile_two_predecessor_join(late_writes_lww=True, first_added_first=True).ainvoke(
        _join_seed()
    )
    first_last = await _compile_two_predecessor_join(late_writes_lww=False, first_added_first=False).ainvoke(
        _join_seed()
    )
    assert late_last["case_review_inbox"] == first_last["case_review_inbox"]
    assert late_last["current_trigger"] == first_last["current_trigger"]
    assert late_last["current_trigger"]["predecessor"] == "review-round-advance"
    assert late_last["case_review_inbox"]["arrivals"] == first_last["case_review_inbox"]["arrivals"]


async def test_compiled_graph_replay_of_same_arrival_id_is_deduplicated() -> None:
    arrival = _arrival("review-round-advance-rework-retry", sequence=4)

    def _offer(state: dict[str, Any]) -> dict[str, object]:
        inbox = offer_case_review_arrival(
            state.get("case_review_inbox") or empty_case_review_inbox(), arrival
        )
        return {"case_review_inbox": inbox, "current_trigger": arrival}

    builder: StateGraph[IntakeState] = StateGraph(IntakeState)
    builder.add_node("offer", cast(Callable[..., Any], _offer))
    builder.add_node("advance-join", cast(Callable[..., Any], advance_join))
    builder.add_edge(START, "offer")
    builder.add_edge("offer", "advance-join")
    builder.add_edge("advance-join", END)
    graph = builder.compile(checkpointer=None)
    first = await graph.ainvoke(cast(Any, _join_seed()))
    replayed = await graph.ainvoke(cast(Any, first))
    assert replayed["case_review_inbox"]["arrivals"] == [arrival]
    assert replayed["current_trigger"] == arrival
    assert replayed["case_review_inbox"]["current_trigger"] == arrival


async def test_compiled_graph_dispatch_cursor_never_reclaims_consumed_arrival() -> None:
    arrival = _arrival("review-round-advance", sequence=1)
    consumed = consume_case_review_trigger(offer_case_review_arrival(empty_case_review_inbox(), arrival))
    replayed = offer_case_review_arrival(consumed, arrival)
    assert replayed["current_trigger"] is None
    with pytest.raises((ValueError, Exception), match="current_trigger.value"):
        await _compile_join_graph().ainvoke(
            {
                **_join_seed(),
                "current_trigger": arrival,
                "case_review_inbox": replayed,
            }
        )


async def test_compiled_case_first_arrival_is_exact_current_trigger() -> None:
    harness = GraphHarness()
    bundle = build_intake_graphs(
        harness.recording_context(owner_id="assurance.intake", contracts=_contracts())
    )
    result = await harness.run(
        bundle.case,
        input=_prepare_input(),
        script={
            "intake.intake": [committed(_artifact(), _RECEIPT)],
            "intake.explore": [committed(_artifact(), _RECEIPT)],
            "intake.case-design": [committed(_design(), _RECEIPT), committed(_design(), _RECEIPT)],
            "intake.case-review": [
                committed(_review("needs_fix", auto_fix=True, used=0), _RECEIPT),
                committed(_review("pass", used=1), _RECEIPT),
            ],
        },
    )
    terminal = result.terminal
    assert isinstance(terminal, dict)
    inbox = terminal["case_review_inbox"]
    current = inbox["current_trigger"]
    assert current is not None
    assert current["predecessor"] == "review-round-advance"
    assert current["business_epoch"] == 0
    assert current["value"] == {"rounds_used": 1, "rounds_budget": 2}
    assert terminal["current_trigger"] == current
    assert terminal["rounds_used"] == current["value"]["rounds_used"]
    assert [call.semantic_node_id for call in result.semantic_calls].count("intake.case-design") == 2


async def test_compiled_case_repeated_epochs_preserve_exact_rounds() -> None:
    harness = GraphHarness()
    bundle = build_intake_graphs(
        harness.recording_context(owner_id="assurance.intake", contracts=_contracts())
    )
    result = await harness.run(
        bundle.case,
        input=_prepare_input(),
        script={
            "intake.intake": [committed(_artifact(), _RECEIPT)],
            "intake.explore": [committed(_artifact(), _RECEIPT)],
            "intake.case-design": [
                committed(_design(), _RECEIPT),
                committed(_design(), _RECEIPT),
                committed(_design(), _RECEIPT),
            ],
            "intake.case-review": [
                committed(_review("needs_fix", auto_fix=True, used=0), _RECEIPT),
                committed(_review("needs_fix", auto_fix=True, used=1), _RECEIPT),
                committed(_review("pass", used=2), _RECEIPT),
            ],
        },
    )
    terminal = result.terminal
    assert isinstance(terminal, dict)
    current = terminal["case_review_inbox"]["current_trigger"]
    assert current is not None
    assert current["value"] == {"rounds_used": 2, "rounds_budget": 2}
    assert current["value"] != {"rounds_used": 3, "rounds_budget": 2}
    assert current["business_epoch"] == 1
    assert terminal["rounds_used"] == 2


def test_arrival_contains_required_identity_fields() -> None:
    arrival = _arrival("review-round-advance", epoch=2, sequence=3, used=1)
    assert set(arrival) == {
        "business_epoch",
        "predecessor",
        "source_activation",
        "sequence",
        "value",
        "arrival_id",
    }
    assert arrival["source_activation"]
    assert arrival["arrival_id"] == arrival["arrival_id"].strip()
    inbox = empty_case_review_inbox()
    assert set(inbox) == {"arrivals", "dispatched_ids", "current_trigger"}
