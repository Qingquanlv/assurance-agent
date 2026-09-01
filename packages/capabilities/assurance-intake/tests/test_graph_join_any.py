from __future__ import annotations

from typing import Any

from assurance_intake.graphs.nodes import apply_current_trigger, select_case_design_retry
from assurance_intake.graphs.state import (
    CASE_REVIEW_PREDECESSORS,
    CaseReviewArrival,
    consume_case_review_trigger,
    empty_case_review_inbox,
    make_case_review_arrival,
    merge_case_review_inbox,
    offer_case_review_arrival,
)

_PREDECESSORS = (
    "review-round-advance",
    "review-round-advance-retry",
    "review-round-advance-rework-retry",
)


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
