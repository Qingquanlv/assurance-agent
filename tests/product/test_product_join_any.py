from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, cast

import pytest
from langgraph.graph import END, START, StateGraph

from assurance_product.graphs.execute import (
    apply_assessment_trigger,
    apply_coverage_needed_trigger,
    apply_failed_join_trigger,
    coverage_needed_join,
    failed_join,
)
from assurance_product.graphs.factory import build_product_graphs
from assurance_product.graphs.state import (
    COVERAGE_NEEDED_PREDECESSORS,
    FAILED_JOIN_PREDECESSORS,
    JoinArrival,
    ProductState,
    consume_coverage_needed_trigger,
    consume_failed_join_trigger,
    empty_coverage_needed_inbox,
    empty_failed_join_inbox,
    make_assessment_trigger,
    make_coverage_needed_arrival,
    make_failed_join_arrival,
    merge_assessment_trigger,
    merge_coverage_needed_inbox,
    merge_failed_join_inbox,
    offer_coverage_needed_arrival,
    offer_failed_join_arrival,
)

from tests.architecture.loop_scc_inventory import LOOP_SCC_INVENTORY
from tests.product.test_product_stategraph_flow import (
    _build_context,
    _flow_features,
    _public_input,
)

_FAILED_JOIN_ROW = (
    "assurance.product.workflow.graph.product-execute",
    "failed-join",
)
_COVERAGE_NEEDED_ROW = (
    "assurance.product.workflow.graph.product-execute",
    "coverage-needed",
)
_CURRENT_TRIGGER_ROWS = (_FAILED_JOIN_ROW, _COVERAGE_NEEDED_ROW)


def _failed_arrival(
    predecessor: str,
    *,
    epoch: int = 0,
    sequence: int = 1,
    used: int = 1,
    budget: int = 2,
) -> JoinArrival:
    return make_failed_join_arrival(
        predecessor=predecessor,
        business_epoch=epoch,
        sequence=sequence,
        value={"rounds_used": used, "rounds_budget": budget},
        source_activation=f"{predecessor}-{epoch}-{sequence}",
    )


def _coverage_arrival(
    predecessor: str,
    *,
    epoch: int = 0,
    sequence: int = 1,
    used: int = 1,
    budget: int = 2,
) -> JoinArrival:
    return make_coverage_needed_arrival(
        predecessor=predecessor,
        business_epoch=epoch,
        sequence=sequence,
        value={"rounds_used": used, "rounds_budget": budget},
        source_activation=f"{predecessor}-{epoch}-{sequence}",
    )


def test_failed_join_predecessors_are_execute_and_run() -> None:
    assert FAILED_JOIN_PREDECESSORS == ("execute", "run")
    assert set(FAILED_JOIN_PREDECESSORS) == {"execute", "run"}


def test_coverage_needed_predecessors_are_quality_and_quality_recheck() -> None:
    assert COVERAGE_NEEDED_PREDECESSORS == ("quality", "quality-recheck")


def test_failed_join_first_arrival_is_exact_current_trigger() -> None:
    arrival = _failed_arrival("execute", used=1, budget=2)
    inbox = offer_failed_join_arrival(empty_failed_join_inbox(), arrival)
    current = inbox["current_trigger"]
    assert current == arrival
    assert inbox["arrivals"] == [arrival]
    assert inbox["dispatched_ids"] == []
    assert current is not None
    assert current["value"] == {"rounds_used": 1, "rounds_budget": 2}
    applied = apply_failed_join_trigger({"failed_join_inbox": inbox, "rounds_used": 0, "rounds_budget": 9})
    assert applied["current_trigger"] == arrival
    assert applied["rounds_used"] == 1
    assert applied["rounds_budget"] == 2


def test_failed_join_late_same_epoch_is_retained_and_dispatched_once() -> None:
    first = _failed_arrival("execute", epoch=0, sequence=1, used=1)
    late = _failed_arrival("run", epoch=0, sequence=2, used=1)
    inbox = offer_failed_join_arrival(empty_failed_join_inbox(), first)
    inbox = offer_failed_join_arrival(inbox, late)
    assert inbox["current_trigger"] == first
    assert inbox["arrivals"] == [first, late]
    first_dispatch = consume_failed_join_trigger(inbox)
    assert first_dispatch["current_trigger"] == late
    assert first["arrival_id"] in first_dispatch["dispatched_ids"]
    assert late["arrival_id"] not in first_dispatch["dispatched_ids"]
    second_dispatch = consume_failed_join_trigger(first_dispatch)
    assert second_dispatch["current_trigger"] is None
    assert set(second_dispatch["dispatched_ids"]) == {first["arrival_id"], late["arrival_id"]}
    replay_late = offer_failed_join_arrival(second_dispatch, late)
    assert replay_late["current_trigger"] is None
    assert replay_late["dispatched_ids"].count(late["arrival_id"]) == 1


def test_failed_join_replay_of_same_arrival_id_is_deduplicated() -> None:
    arrival = _failed_arrival("run", sequence=4)
    inbox = offer_failed_join_arrival(empty_failed_join_inbox(), arrival)
    replayed = offer_failed_join_arrival(inbox, arrival)
    assert replayed["arrivals"] == [arrival]
    assert replayed["current_trigger"] == arrival


def test_failed_join_two_reducer_merge_orders_are_identical() -> None:
    first = _failed_arrival("execute", sequence=1, used=1)
    second = _failed_arrival("run", sequence=2, used=1)
    empty = empty_failed_join_inbox()
    left = merge_failed_join_inbox(
        offer_failed_join_arrival(empty, first),
        offer_failed_join_arrival(empty, second),
    )
    right = merge_failed_join_inbox(
        offer_failed_join_arrival(empty, second),
        offer_failed_join_arrival(empty, first),
    )
    assert left == right
    assert left["arrivals"] == [first, second]
    assert left["current_trigger"] == first


def test_failed_join_repeated_epochs_preserve_exact_rounds() -> None:
    epoch_one = _failed_arrival("execute", epoch=0, sequence=1, used=1, budget=2)
    epoch_two = _failed_arrival("run", epoch=1, sequence=1, used=2, budget=2)
    inbox = consume_failed_join_trigger(offer_failed_join_arrival(empty_failed_join_inbox(), epoch_one))
    inbox = offer_failed_join_arrival(inbox, epoch_two)
    current = inbox["current_trigger"]
    assert current == epoch_two
    assert current is not None
    assert current["value"] == {"rounds_used": 2, "rounds_budget": 2}
    assert current["value"] != {"rounds_used": 3, "rounds_budget": 2}
    assert current["business_epoch"] == 1


def test_failed_join_dispatch_cursor_never_reclaims_consumed_arrival() -> None:
    arrival = _failed_arrival("execute", sequence=1)
    inbox = consume_failed_join_trigger(offer_failed_join_arrival(empty_failed_join_inbox(), arrival))
    assert inbox["current_trigger"] is None
    reclaimed = offer_failed_join_arrival(inbox, arrival)
    assert reclaimed["current_trigger"] is None
    assert reclaimed["dispatched_ids"] == [arrival["arrival_id"]]


def test_coverage_needed_first_arrival_is_exact_current_trigger() -> None:
    arrival = _coverage_arrival("quality", used=1, budget=2)
    inbox = offer_coverage_needed_arrival(empty_coverage_needed_inbox(), arrival)
    assert inbox["current_trigger"] == arrival
    applied = apply_coverage_needed_trigger(
        {"coverage_needed_inbox": inbox, "rounds_used": 0, "rounds_budget": 9}
    )
    assert applied["current_trigger"] == arrival
    assert applied["rounds_used"] == 1
    assert applied["rounds_budget"] == 2


def test_coverage_needed_late_same_epoch_is_retained_and_dispatched_once() -> None:
    first = _coverage_arrival("quality", epoch=0, sequence=1, used=1)
    late = _coverage_arrival("quality-recheck", epoch=0, sequence=2, used=1)
    inbox = offer_coverage_needed_arrival(empty_coverage_needed_inbox(), first)
    inbox = offer_coverage_needed_arrival(inbox, late)
    assert inbox["current_trigger"] == first
    assert inbox["arrivals"] == [first, late]
    first_dispatch = consume_coverage_needed_trigger(inbox)
    assert first_dispatch["current_trigger"] == late
    finished = consume_coverage_needed_trigger(first_dispatch)
    assert finished["current_trigger"] is None
    replay = offer_coverage_needed_arrival(finished, late)
    assert replay["current_trigger"] is None
    assert replay["dispatched_ids"].count(late["arrival_id"]) == 1


def test_coverage_needed_replay_merge_epochs_and_cursor() -> None:
    first = _coverage_arrival("quality", sequence=1, used=1)
    second = _coverage_arrival("quality-recheck", sequence=2, used=1)
    empty = empty_coverage_needed_inbox()
    left = merge_coverage_needed_inbox(
        offer_coverage_needed_arrival(empty, first),
        offer_coverage_needed_arrival(empty, second),
    )
    right = merge_coverage_needed_inbox(
        offer_coverage_needed_arrival(empty, second),
        offer_coverage_needed_arrival(empty, first),
    )
    assert left == right
    replayed = offer_coverage_needed_arrival(left, first)
    assert replayed["arrivals"] == [first, second]
    epoch_two = _coverage_arrival("quality-recheck", epoch=1, sequence=1, used=2, budget=2)
    consumed = consume_coverage_needed_trigger(offer_coverage_needed_arrival(empty, first))
    nxt = offer_coverage_needed_arrival(consumed, epoch_two)
    current = nxt["current_trigger"]
    assert current == epoch_two
    assert current is not None
    assert current["value"] == {"rounds_used": 2, "rounds_budget": 2}
    leftover = consume_coverage_needed_trigger(nxt)
    assert leftover["current_trigger"] is None
    assert offer_coverage_needed_arrival(leftover, epoch_two)["current_trigger"] is None


def _compile_failed_join_graph() -> Any:
    builder: StateGraph[ProductState] = StateGraph(ProductState)
    builder.add_node("failed-join", cast(Callable[..., Any], failed_join))
    builder.add_edge(START, "failed-join")
    builder.add_edge("failed-join", END)
    return builder.compile(checkpointer=None)


def _compile_coverage_join_graph() -> Any:
    builder: StateGraph[ProductState] = StateGraph(ProductState)
    builder.add_node("coverage-needed", cast(Callable[..., Any], coverage_needed_join))
    builder.add_edge(START, "coverage-needed")
    builder.add_edge("coverage-needed", END)
    return builder.compile(checkpointer=None)


def _offer_failed(arrival: JoinArrival, *, poison_lww: bool) -> Callable[..., dict[str, object]]:
    def _offer(state: Mapping[str, Any]) -> dict[str, object]:
        inbox = offer_failed_join_arrival(
            state.get("failed_join_inbox") or empty_failed_join_inbox(), arrival
        )
        update: dict[str, object] = {"failed_join_inbox": inbox}
        if poison_lww:
            update["current_trigger"] = arrival
        return update

    return _offer


def _compile_two_predecessor_failed_join(*, late_writes_lww: bool, first_added_first: bool) -> Any:
    first = _failed_arrival("execute", epoch=0, sequence=1, used=1)
    late = _failed_arrival("run", epoch=0, sequence=1, used=1)
    nodes = [
        ("execute", _offer_failed(first, poison_lww=not late_writes_lww)),
        ("run", _offer_failed(late, poison_lww=late_writes_lww)),
    ]
    ordered = nodes if first_added_first else list(reversed(nodes))
    builder: StateGraph[ProductState] = StateGraph(ProductState)
    for name, node in ordered:
        builder.add_node(name, cast(Callable[..., Any], node))
    builder.add_node("failed-join", cast(Callable[..., Any], failed_join))
    builder.add_edge(START, "execute")
    builder.add_edge(START, "run")
    builder.add_edge("execute", "failed-join")
    builder.add_edge("run", "failed-join")
    builder.add_edge("failed-join", END)
    return builder.compile(checkpointer=None)


def _join_seed() -> dict[str, object]:
    return {
        "change_id": "CH-DEMO-001",
        "requirement": "Add login",
        "selected_test_families": ["api"],
        "capability_leafs": ["entities.item.create"],
        "allowed_artifact_paths": ["qa/changes"],
        "rounds_used": 0,
        "rounds_budget": 2,
    }


def test_compiled_failed_join_reads_inbox_cursor_not_lww_shadow() -> None:
    first = _failed_arrival("execute", epoch=0, sequence=1, used=1)
    late = _failed_arrival("run", epoch=0, sequence=2, used=9, budget=9)
    inbox = offer_failed_join_arrival(empty_failed_join_inbox(), first)
    inbox = offer_failed_join_arrival(inbox, late)
    result = _compile_failed_join_graph().invoke(
        {
            **_join_seed(),
            "current_trigger": late,
            "failed_join_inbox": inbox,
            "rounds_used": 0,
            "rounds_budget": 2,
        }
    )
    assert result["current_trigger"] == first
    assert result["failed_join_inbox"]["current_trigger"] == first
    assert result["failed_join_inbox"]["arrivals"] == [first, late]
    assert result["rounds_used"] == 1
    assert result["rounds_budget"] == 2
    assert result["current_trigger"] != late


def test_compiled_failed_join_same_epoch_then_late_dispatched() -> None:
    first = _failed_arrival("execute", epoch=0, sequence=1, used=1)
    late = _failed_arrival("run", epoch=0, sequence=1, used=1)
    result = _compile_two_predecessor_failed_join(late_writes_lww=True, first_added_first=True).invoke(
        _join_seed()
    )
    inbox = result["failed_join_inbox"]
    assert [item["arrival_id"] for item in inbox["arrivals"]] == [first["arrival_id"], late["arrival_id"]]
    assert inbox["current_trigger"] == first
    consumed = consume_failed_join_trigger(inbox)
    late_result = _compile_failed_join_graph().invoke(
        {**result, "current_trigger": first, "failed_join_inbox": consumed}
    )
    assert late_result["current_trigger"] == late
    finished = consume_failed_join_trigger(late_result["failed_join_inbox"])
    assert finished["current_trigger"] is None
    replay = offer_failed_join_arrival(finished, late)
    assert replay["current_trigger"] is None


def test_compiled_failed_join_two_reducer_merge_orders_are_identical() -> None:
    late_last = _compile_two_predecessor_failed_join(late_writes_lww=True, first_added_first=True).invoke(
        _join_seed()
    )
    first_last = _compile_two_predecessor_failed_join(late_writes_lww=False, first_added_first=False).invoke(
        _join_seed()
    )
    assert late_last["failed_join_inbox"] == first_last["failed_join_inbox"]
    assert late_last["current_trigger"] == first_last["current_trigger"]
    assert late_last["current_trigger"]["predecessor"] == "execute"


def test_compiled_coverage_needed_reads_inbox_cursor_not_lww() -> None:
    first = _coverage_arrival("quality", epoch=0, sequence=1, used=1)
    late = _coverage_arrival("quality-recheck", epoch=0, sequence=2, used=9, budget=9)
    inbox = offer_coverage_needed_arrival(empty_coverage_needed_inbox(), first)
    inbox = offer_coverage_needed_arrival(inbox, late)
    result = _compile_coverage_join_graph().invoke(
        {
            **_join_seed(),
            "current_trigger": late,
            "coverage_needed_inbox": inbox,
            "rounds_used": 0,
            "rounds_budget": 2,
        }
    )
    assert result["current_trigger"] == first
    assert result["rounds_used"] == 1
    assert result["current_trigger"] != late


def test_initial_satisfied_or_unsatisfied_terminates_without_repair() -> None:
    satisfied = (
        build_product_graphs(context=_build_context(), features=_flow_features())
        .entrypoints["execute"]
        .invoke(_public_input("execute"))
    )
    assert satisfied["terminal"] == "done"
    assert satisfied["assessment_trigger"]["source"] == "quality"
    assert satisfied["assessment_trigger"]["coverage_state"] == "satisfied"
    inbox = satisfied.get("coverage_needed_inbox") or empty_coverage_needed_inbox()
    assert list(inbox.get("arrivals") or []) == []

    unsatisfied = (
        build_product_graphs(
            context=_build_context(),
            features=_flow_features(
                assess={"coverage_state": "exhausted", "rounds_used": 0, "rounds_budget": 0}
            ),
        )
        .entrypoints["execute"]
        .invoke(_public_input("execute"))
    )
    assert unsatisfied["terminal"] == "not-achieved"
    assert unsatisfied["assessment_trigger"]["coverage_state"] == "exhausted"
    unsatisfied_inbox = unsatisfied.get("coverage_needed_inbox") or empty_coverage_needed_inbox()
    assert list(unsatisfied_inbox.get("arrivals") or []) == []


def test_quality_recheck_is_only_after_repair_required_and_exit_schedules_no_later_recheck() -> None:
    graphs = build_product_graphs(
        context=_build_context(),
        features=_flow_features(
            assess=(
                {"coverage_state": "repair_required", "rounds_used": 0, "rounds_budget": 2},
                {"coverage_state": "satisfied", "rounds_used": 1, "rounds_budget": 2},
            ),
            repair_coverage={"status": "repaired", "kind": "coverage", "rounds_used": 1, "rounds_budget": 2},
        ),
    )
    result = graphs.entrypoints["execute"].invoke(_public_input("execute"))
    trigger = result["assessment_trigger"]
    assert trigger["source"] == "quality-recheck"
    assert trigger["coverage_state"] == "satisfied"
    inbox = result["coverage_needed_inbox"]
    assert inbox["arrivals"][0]["predecessor"] == "quality"
    assert inbox["current_trigger"]["predecessor"] == "quality"
    assert result["terminal"] == "done"
    assert (
        all(item["predecessor"] != "quality-recheck" for item in inbox["arrivals"][1:])
        or len(inbox["arrivals"]) == 1
    )


def test_quality_and_quality_recheck_cannot_both_reach_the_same_exit() -> None:
    first = make_assessment_trigger(
        source="quality",
        coverage_state="satisfied",
        rounds={"rounds_used": 0, "rounds_budget": 2},
        evidence=[],
    )
    late = make_assessment_trigger(
        source="quality-recheck",
        coverage_state="satisfied",
        rounds={"rounds_used": 1, "rounds_budget": 2},
        evidence=[],
    )
    with pytest.raises((ValueError, TypeError), match="exclusive|late|source|both"):
        merge_assessment_trigger(first, late)
    with pytest.raises((ValueError, TypeError), match="exclusive|late|source|both"):
        apply_assessment_trigger({"assessment_trigger": first}, late)


def test_late_reactivation_after_assessment_exit_is_rejected() -> None:
    first = make_assessment_trigger(
        source="quality",
        coverage_state="exhausted",
        rounds={"rounds_used": 0, "rounds_budget": 0},
        evidence=[],
    )
    late = make_assessment_trigger(
        source="quality-recheck",
        coverage_state="exhausted",
        rounds={"rounds_used": 1, "rounds_budget": 2},
        evidence=[],
    )
    with pytest.raises((ValueError, TypeError), match="exclusive|late|source|both"):
        apply_assessment_trigger({"assessment_trigger": first, "terminal": "not-achieved"}, late)


def test_crafted_update_claiming_both_assessment_outcomes_fails_closed() -> None:
    with pytest.raises((ValueError, TypeError), match="exclusive|both|outcome"):
        apply_assessment_trigger(
            {
                "assess_satisfied": True,
                "assess_unsatisfied": True,
                "coverage_state": "satisfied",
            },
            make_assessment_trigger(
                source="quality",
                coverage_state="satisfied",
                rounds={"rounds_used": 0, "rounds_budget": 1},
                evidence=[],
            ),
        )
    with pytest.raises((ValueError, TypeError), match="exclusive|both|outcome"):
        merge_assessment_trigger(
            make_assessment_trigger(
                source="quality",
                coverage_state="satisfied",
                rounds={"rounds_used": 0, "rounds_budget": 1},
                evidence=[],
            ),
            make_assessment_trigger(
                source="quality",
                coverage_state="exhausted",
                rounds={"rounds_used": 0, "rounds_budget": 1},
                evidence=[],
            ),
        )


def test_assessment_trigger_identity_and_at_most_one_arrival() -> None:
    trigger = make_assessment_trigger(
        source="quality",
        coverage_state="satisfied",
        rounds={"rounds_used": 0, "rounds_budget": 1},
        evidence=[{"path": "qa/changes", "digest": "a" * 64}],
    )
    assert set(trigger) == {"source", "coverage_state", "rounds", "evidence"}
    applied = apply_assessment_trigger({}, trigger)
    assert applied["assessment_trigger"] == trigger
    replayed = apply_assessment_trigger(applied, trigger)
    assert replayed["assessment_trigger"] == trigger


@pytest.mark.parametrize(
    "row",
    _CURRENT_TRIGGER_ROWS,
    ids=lambda row: f"{row[0]}/{row[1]}",
)
def test_current_trigger(row: tuple[str, str]) -> None:
    graph_id, anchor = row
    assert (graph_id, anchor) in {item.anchor for item in LOOP_SCC_INVENTORY}
    if anchor == "failed-join":
        arrival = _failed_arrival("execute", used=1, budget=2)
        inbox = offer_failed_join_arrival(empty_failed_join_inbox(), arrival)
        applied = apply_failed_join_trigger(
            {
                "current_trigger": _failed_arrival("run", used=9, budget=9),
                "failed_join_inbox": inbox,
                "rounds_used": 0,
                "rounds_budget": 2,
            }
        )
        assert applied["current_trigger"] == arrival
        assert applied["rounds_used"] == 1
        assert applied["rounds_budget"] == 2
        return
    arrival = _coverage_arrival("quality", used=1, budget=2)
    inbox = offer_coverage_needed_arrival(empty_coverage_needed_inbox(), arrival)
    applied = apply_coverage_needed_trigger(
        {
            "current_trigger": _coverage_arrival("quality-recheck", used=9, budget=9),
            "coverage_needed_inbox": inbox,
            "rounds_used": 0,
            "rounds_budget": 2,
        }
    )
    assert applied["current_trigger"] == arrival
    assert applied["rounds_used"] == 1
    assert applied["rounds_budget"] == 2


def test_loop_scc_product_rows_point_to_this_current_trigger_matrix() -> None:
    product = [row for row in LOOP_SCC_INVENTORY if row.graph_id.endswith("product-execute")]
    assert {row.anchor_node_id for row in product} == {"failed-join", "coverage-needed"}
    assert all(row.target_test.startswith("tests/product/test_product_join_any.py") for row in product)
