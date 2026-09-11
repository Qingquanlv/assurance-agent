from __future__ import annotations

from collections.abc import Callable
from itertools import permutations
from typing import Any, cast

import pytest
from langgraph.graph import END, START, StateGraph

from assurance_generation.contracts.attempts import AGENT_JOB_CONTRACTS, TASK_ATTEMPT_CONTRACTS
from assurance_generation.graphs.factory import build_generation_graphs
from assurance_generation.graphs.nodes import apply_current_trigger, complete_generation_node, plan_round_join
from assurance_generation.graphs.state import (
    GENERATION_FAMILIES,
    PLAN_ROUND_PREDECESSORS,
    FamilyLaneResult,
    GenerationState,
    PlanRoundArrival,
    consume_plan_round_trigger,
    empty_plan_round_inbox,
    make_family_lane_result,
    make_plan_round_arrival,
    merge_family_results,
    merge_plan_round_inbox,
    offer_plan_round_arrival,
)
from graph_engine.attempts.contracts import TaskAttemptContract
from graph_engine.attempts.resolutions import ReceiptRef
from graph_engine.testing import GraphHarness, committed

_FAMILIES = ("api", "e2e", "fuzz", "performance")
_CURRENT_TRIGGER_ROWS = (
    ("assurance.generation.workflow.graph.generation-api", "plan-round-join"),
    ("assurance.generation.workflow.graph.generation-e2e", "plan-round-join"),
    ("assurance.generation.workflow.graph.generation-fuzz", "plan-round-join"),
    ("assurance.generation.workflow.graph.generation-performance", "plan-round-join"),
)
_SHA = "a" * 64
_RECEIPT = ReceiptRef(receipt_id="receipt-1", receipt_digest=_SHA)


def _arrival(
    predecessor: str,
    *,
    epoch: int = 0,
    sequence: int = 1,
    used: int = 1,
    budget: int = 2,
) -> PlanRoundArrival:
    return make_plan_round_arrival(
        predecessor=predecessor,
        business_epoch=epoch,
        sequence=sequence,
        value={"rounds_used": used, "rounds_budget": budget},
        source_activation=f"{predecessor}-{epoch}-{sequence}",
    )


def _result(
    family: str,
    *,
    selected: bool = True,
    receipt_id: str | None = None,
    coverage_epoch: int = 0,
) -> FamilyLaneResult:
    return make_family_lane_result(
        coverage_epoch=coverage_epoch,
        family=family,
        receipt_id=receipt_id or f"receipt-{family}",
        selected=selected,
        status="passed" if selected else "skipped",
    )


def _contracts() -> dict[str, TaskAttemptContract[Any, Any]]:
    contracts = {
        contract.contract_id: contract.to_task_contract() for contract in AGENT_JOB_CONTRACTS.values()
    }
    contracts.update({contract.contract_id: contract for contract in TASK_ATTEMPT_CONTRACTS.values()})
    return contracts


def _family_input(family: str) -> dict[str, object]:
    return {
        "change_id": "CH-DEMO-001",
        "plan_digest": _SHA,
        "plan_ref": {
            "path": f"qa/results/plan/{_SHA}/resolved-assurance-plan.json",
            "digest": _SHA,
        },
        "selected_test_families": [family],
        "capability_leafs": ["entities.item.create"],
        "allowed_artifact_paths": ["qa/cases", "qa/fixtures", "qa/results", "qa/tests"],
        "family": family,
        "lane_selected": True,
        "rounds_used": 0,
        "rounds_budget": 2,
        "review_stage": "plan",
    }


def _plan() -> dict[str, object]:
    return {"artifacts": [{"path": "qa/results", "digest": _SHA}]}


def _review(
    decision: str,
    *,
    auto_fix: bool = False,
    used: int | None = 0,
    budget: int | None = 2,
    readiness: str = "ready",
) -> dict[str, object]:
    payload: dict[str, object] = {
        "decision": decision,
        "auto_fix_allowed": auto_fix,
        "human_review_required": False,
        "codegen_readiness": readiness,
        "artifacts": [{"path": "qa/results", "digest": _SHA}],
    }
    if used is not None:
        payload["rounds_used"] = used
    if budget is not None:
        payload["rounds_budget"] = budget
    return payload


def _codegen(family: str) -> dict[str, object]:
    if family in {"api", "e2e"}:
        return {"schema_version": "2", "verdict": "accepted"}
    return {"schema_version": "1", "needs_fix": False}


def _family_graph(bundle: object, family: str) -> object:
    return getattr(bundle, family)


def _compile_family_join_graph(family: str) -> Any:
    builder: StateGraph[GenerationState] = StateGraph(GenerationState)
    join_name = f"{family}-plan-round-join"
    builder.add_node(join_name, cast(Callable[..., Any], plan_round_join))
    builder.add_edge(START, join_name)
    builder.add_edge(join_name, END)
    return builder.compile(checkpointer=None)


def _offer_node(arrival: PlanRoundArrival, *, poison_lww: bool) -> Callable[..., dict[str, object]]:
    def _offer(state: dict[str, Any]) -> dict[str, object]:
        inbox = offer_plan_round_arrival(state.get("plan_round_inbox") or empty_plan_round_inbox(), arrival)
        update: dict[str, object] = {"plan_round_inbox": inbox}
        if poison_lww:
            update["current_trigger"] = arrival
        return update

    return _offer


def _compile_family_two_predecessor_join(
    family: str, *, late_writes_lww: bool, first_added_first: bool
) -> Any:
    first = _arrival("plan-review-round-advance", epoch=0, sequence=1, used=1)
    late = _arrival("plan-review-round-advance-retry", epoch=0, sequence=1, used=1)
    join_name = f"{family}-plan-round-join"
    nodes = [
        (f"{family}-plan-review-round-advance", _offer_node(first, poison_lww=not late_writes_lww)),
        (f"{family}-plan-review-round-advance-retry", _offer_node(late, poison_lww=late_writes_lww)),
    ]
    ordered = nodes if first_added_first else list(reversed(nodes))
    builder: StateGraph[GenerationState] = StateGraph(GenerationState)
    for name, node in ordered:
        builder.add_node(name, cast(Callable[..., Any], node))
    builder.add_node(join_name, cast(Callable[..., Any], plan_round_join))
    builder.add_edge(START, f"{family}-plan-review-round-advance")
    builder.add_edge(START, f"{family}-plan-review-round-advance-retry")
    builder.add_edge(f"{family}-plan-review-round-advance", join_name)
    builder.add_edge(f"{family}-plan-review-round-advance-retry", join_name)
    builder.add_edge(join_name, END)
    return builder.compile(checkpointer=None)


def _join_seed(family: str) -> dict[str, object]:
    return {
        "change_id": "CH-DEMO-001",
        "selected_test_families": [family],
        "capability_leafs": ["entities.item.create"],
        "allowed_artifact_paths": ["qa/cases", "qa/fixtures", "qa/results", "qa/tests"],
        "family": family,
        "rounds_used": 0,
        "rounds_budget": 2,
    }


def test_join_predecessors_are_the_two_plan_advance_sites() -> None:
    assert PLAN_ROUND_PREDECESSORS == (
        "plan-review-round-advance",
        "plan-review-round-advance-retry",
    )


def test_first_arrival_becomes_exact_current_trigger() -> None:
    arrival = _arrival("plan-review-round-advance", used=1, budget=2)
    inbox = offer_plan_round_arrival(empty_plan_round_inbox(), arrival)
    current = inbox["current_trigger"]
    assert current == arrival
    assert inbox["arrivals"] == [arrival]
    assert inbox["dispatched_ids"] == []
    assert current is not None
    assert current["value"] == {"rounds_used": 1, "rounds_budget": 2}
    assert current["business_epoch"] == 0
    assert current["predecessor"] == "plan-review-round-advance"
    assert current["arrival_id"] == arrival["arrival_id"]


def test_late_second_arrival_in_same_epoch_is_retained_and_dispatched_once() -> None:
    first = _arrival("plan-review-round-advance", epoch=0, sequence=1, used=1)
    late = _arrival("plan-review-round-advance-retry", epoch=0, sequence=2, used=1)
    inbox = offer_plan_round_arrival(empty_plan_round_inbox(), first)
    inbox = offer_plan_round_arrival(inbox, late)
    assert inbox["current_trigger"] == first
    assert inbox["arrivals"] == [first, late]
    first_dispatch = consume_plan_round_trigger(inbox)
    assert first_dispatch["current_trigger"] == late
    assert first["arrival_id"] in first_dispatch["dispatched_ids"]
    assert late["arrival_id"] not in first_dispatch["dispatched_ids"]
    second_dispatch = consume_plan_round_trigger(first_dispatch)
    assert second_dispatch["current_trigger"] is None
    assert set(second_dispatch["dispatched_ids"]) == {first["arrival_id"], late["arrival_id"]}
    replay_late = offer_plan_round_arrival(second_dispatch, late)
    assert replay_late["current_trigger"] is None
    assert replay_late["dispatched_ids"].count(late["arrival_id"]) == 1


def test_replay_of_the_same_arrival_id_is_deduplicated() -> None:
    arrival = _arrival("plan-review-round-advance-retry", sequence=4)
    inbox = offer_plan_round_arrival(empty_plan_round_inbox(), arrival)
    replayed = offer_plan_round_arrival(inbox, arrival)
    assert replayed["arrivals"] == [arrival]
    assert replayed["current_trigger"] == arrival


def test_two_reducer_merge_orders_produce_identical_inbox_state() -> None:
    first = _arrival("plan-review-round-advance", sequence=1, used=1)
    second = _arrival("plan-review-round-advance-retry", sequence=2, used=1)
    empty = empty_plan_round_inbox()
    left = merge_plan_round_inbox(
        offer_plan_round_arrival(empty, first),
        offer_plan_round_arrival(empty, second),
    )
    right = merge_plan_round_inbox(
        offer_plan_round_arrival(empty, second),
        offer_plan_round_arrival(empty, first),
    )
    assert left == right
    assert left["arrivals"] == [first, second]
    assert left["current_trigger"] == first


def test_repeated_business_epochs_preserve_exact_rounds_used_and_budget() -> None:
    epoch_one = _arrival("plan-review-round-advance", epoch=0, sequence=1, used=1, budget=2)
    epoch_two = _arrival("plan-review-round-advance-retry", epoch=1, sequence=1, used=2, budget=2)
    inbox = consume_plan_round_trigger(offer_plan_round_arrival(empty_plan_round_inbox(), epoch_one))
    inbox = offer_plan_round_arrival(inbox, epoch_two)
    current = inbox["current_trigger"]
    assert current == epoch_two
    assert current is not None
    assert current["value"] == {"rounds_used": 2, "rounds_budget": 2}
    assert current["value"] != {"rounds_used": 3, "rounds_budget": 2}
    assert current["business_epoch"] == 1


def test_dispatch_cursor_never_reclaims_a_consumed_arrival() -> None:
    arrival = _arrival("plan-review-round-advance", sequence=1)
    inbox = consume_plan_round_trigger(offer_plan_round_arrival(empty_plan_round_inbox(), arrival))
    assert inbox["current_trigger"] is None
    assert arrival["arrival_id"] in inbox["dispatched_ids"]
    reclaimed = offer_plan_round_arrival(inbox, arrival)
    assert reclaimed["current_trigger"] is None
    assert reclaimed["dispatched_ids"] == [arrival["arrival_id"]]


def test_downstream_plan_retry_reads_current_trigger_value_only() -> None:
    stale = {"rounds_used": 0, "rounds_budget": 2}
    arrival = _arrival("plan-review-round-advance-retry", used=1, budget=2)
    inbox = offer_plan_round_arrival(empty_plan_round_inbox(), arrival)
    state: dict[str, Any] = {
        "family": "api",
        **stale,
        "predecessor_tokens": {"plan-round-join": {"tokens": [{"rounds_used": 9, "rounds_budget": 9}]}},
        "plan_round_inbox": inbox,
    }
    applied = apply_current_trigger(state)
    assert applied["rounds_used"] == 1
    assert applied["rounds_budget"] == 2
    assert applied["current_trigger"] == arrival
    assert applied["rounds_used"] == arrival["value"]["rounds_used"]
    assert applied["rounds_used"] != state["rounds_used"]
    assert "tokens" not in applied


def test_family_result_reducer_is_associative_commutative_and_idempotent() -> None:
    api = _result("api")
    e2e = _result("e2e")
    fuzz = _result("fuzz", selected=False)
    merged = merge_family_results(merge_family_results([api], [e2e]), [fuzz])
    regrouped = merge_family_results([api], merge_family_results([e2e], [fuzz]))
    reversed_merge = merge_family_results([fuzz, e2e], [api])
    replayed = merge_family_results(merged, [api])
    assert merged == regrouped == reversed_merge
    assert replayed == merged
    assert [item["family"] for item in merged] == ["api", "e2e", "fuzz"]


def test_all_family_result_arrival_permutations_merge_identically() -> None:
    results = [_result(family) for family in GENERATION_FAMILIES]
    expected = merge_family_results([], results)
    for order in permutations(results):
        acc: list[FamilyLaneResult] = []
        for item in order:
            acc = merge_family_results(acc, [item])
        assert acc == expected
    replayed = merge_family_results(expected, [_result("api")])
    assert replayed == expected
    assert len(expected) == 4


def test_four_family_superstep_does_not_write_concurrent_scalar_keys() -> None:
    results = [_result(family, selected=family in {"api", "e2e"}) for family in GENERATION_FAMILIES]
    merged = merge_family_results([], results)
    assert {item["family"] for item in merged} == set(GENERATION_FAMILIES)
    for item in merged:
        assert set(item) == {"coverage_epoch", "family", "receipt_id", "selected", "status"}
    complete = complete_generation_node({"family_results": merged, "selected_test_families": ["api", "e2e"]})
    assert set(complete) == {"families", "selected_families"}
    assert complete["selected_families"] == ["api", "e2e"]


def test_completion_runs_only_after_exactly_one_result_for_each_family() -> None:
    three = [_result(family) for family in ("api", "e2e", "fuzz")]
    with pytest.raises(ValueError):
        complete_generation_node({"family_results": three, "selected_test_families": ["api"]})
    four = [*three, _result("performance", selected=False)]
    output = complete_generation_node(
        {"family_results": four, "selected_test_families": ["api", "e2e", "fuzz"]}
    )
    families = output["families"]
    assert isinstance(families, dict)
    assert families["api"] == {"completed": True}
    assert "performance" not in families


def test_old_epoch_family_arrivals_cannot_complete_current_barrier() -> None:
    old = [_result(family, coverage_epoch=0) for family in GENERATION_FAMILIES]
    current = [_result("api", coverage_epoch=1, receipt_id="receipt-api-1")]
    with pytest.raises(ValueError, match="one result for each family"):
        complete_generation_node(
            {
                "coverage_epoch": 1,
                "family_results": [*old, *current],
                "selected_test_families": ["api"],
            }
        )


def test_arrival_contains_required_identity_fields() -> None:
    arrival = _arrival("plan-review-round-advance", epoch=2, sequence=3, used=1)
    assert set(arrival) == {
        "business_epoch",
        "predecessor",
        "source_activation",
        "sequence",
        "value",
        "arrival_id",
    }
    inbox = empty_plan_round_inbox()
    assert set(inbox) == {"arrivals", "dispatched_ids", "current_trigger"}


@pytest.mark.parametrize(
    "row",
    _CURRENT_TRIGGER_ROWS,
    ids=lambda row: f"{row[0]}/{row[1]}",
)
def test_current_trigger(row: tuple[str, str]) -> None:
    graph_id, anchor = row
    family = graph_id.rsplit("generation-", 1)[-1]
    assert anchor == "plan-round-join"
    arrival = _arrival("plan-review-round-advance", used=1, budget=2)
    inbox = offer_plan_round_arrival(empty_plan_round_inbox(), arrival)
    applied = apply_current_trigger(
        {
            "family": family,
            "rounds_used": 0,
            "rounds_budget": 2,
            "current_trigger": _arrival("plan-review-round-advance-retry", used=9, budget=9),
            "plan_round_inbox": inbox,
        }
    )
    assert applied["current_trigger"] == arrival
    assert applied["rounds_used"] == 1
    assert applied["rounds_budget"] == 2


@pytest.mark.parametrize("family", _FAMILIES)
async def test_compiled_join_reads_inbox_cursor_not_lww_shadow(family: str) -> None:
    first = _arrival("plan-review-round-advance", epoch=0, sequence=1, used=1)
    late = _arrival("plan-review-round-advance-retry", epoch=0, sequence=2, used=9, budget=9)
    inbox = offer_plan_round_arrival(empty_plan_round_inbox(), first)
    inbox = offer_plan_round_arrival(inbox, late)
    result = await _compile_family_join_graph(family).ainvoke(
        {
            **_join_seed(family),
            "current_trigger": late,
            "plan_round_inbox": inbox,
            "rounds_used": 0,
            "rounds_budget": 2,
        }
    )
    assert result["family"] == family
    assert result["current_trigger"] == first
    assert result["plan_round_inbox"]["current_trigger"] == first
    assert result["plan_round_inbox"]["arrivals"] == [first, late]
    assert result["rounds_used"] == 1
    assert result["rounds_budget"] == 2
    assert result["current_trigger"] != late


@pytest.mark.parametrize("family", _FAMILIES)
async def test_compiled_graph_same_epoch_arrivals_retained_first_current_then_late_dispatched(
    family: str,
) -> None:
    first = _arrival("plan-review-round-advance", epoch=0, sequence=1, used=1)
    late = _arrival("plan-review-round-advance-retry", epoch=0, sequence=1, used=1)
    result = await _compile_family_two_predecessor_join(
        family, late_writes_lww=True, first_added_first=True
    ).ainvoke(_join_seed(family))
    inbox = result["plan_round_inbox"]
    assert result["family"] == family
    assert [item["arrival_id"] for item in inbox["arrivals"]] == [first["arrival_id"], late["arrival_id"]]
    assert inbox["current_trigger"] == first
    assert result["current_trigger"] == first
    consumed = consume_plan_round_trigger(inbox)
    late_result = await _compile_family_join_graph(family).ainvoke(
        {**result, "current_trigger": first, "plan_round_inbox": consumed}
    )
    assert late_result["family"] == family
    assert late_result["current_trigger"] == late
    finished = consume_plan_round_trigger(late_result["plan_round_inbox"])
    assert finished["current_trigger"] is None
    replay = offer_plan_round_arrival(finished, late)
    assert replay["current_trigger"] is None


@pytest.mark.parametrize("family", _FAMILIES)
async def test_compiled_graph_two_reducer_merge_orders_are_identical(family: str) -> None:
    late_last = await _compile_family_two_predecessor_join(
        family, late_writes_lww=True, first_added_first=True
    ).ainvoke(_join_seed(family))
    first_last = await _compile_family_two_predecessor_join(
        family, late_writes_lww=False, first_added_first=False
    ).ainvoke(_join_seed(family))
    assert late_last["family"] == first_last["family"] == family
    assert late_last["plan_round_inbox"] == first_last["plan_round_inbox"]
    assert late_last["current_trigger"] == first_last["current_trigger"]
    assert late_last["current_trigger"]["predecessor"] == "plan-review-round-advance"


@pytest.mark.parametrize("family", _FAMILIES)
async def test_compiled_graph_replay_of_same_arrival_id_is_deduplicated(family: str) -> None:
    arrival = _arrival("plan-review-round-advance-retry", sequence=4)
    join_name = f"{family}-plan-round-join"

    def _offer(state: dict[str, Any]) -> dict[str, object]:
        inbox = offer_plan_round_arrival(state.get("plan_round_inbox") or empty_plan_round_inbox(), arrival)
        return {"plan_round_inbox": inbox, "current_trigger": arrival}

    builder: StateGraph[GenerationState] = StateGraph(GenerationState)
    builder.add_node(f"{family}-offer", cast(Callable[..., Any], _offer))
    builder.add_node(join_name, cast(Callable[..., Any], plan_round_join))
    builder.add_edge(START, f"{family}-offer")
    builder.add_edge(f"{family}-offer", join_name)
    builder.add_edge(join_name, END)
    graph = builder.compile(checkpointer=None)
    first = await graph.ainvoke(cast(Any, _join_seed(family)))
    replayed = await graph.ainvoke(cast(Any, first))
    assert replayed["family"] == family
    assert replayed["plan_round_inbox"]["arrivals"] == [arrival]
    assert replayed["current_trigger"] == arrival


@pytest.mark.parametrize("family", _FAMILIES)
async def test_compiled_graph_dispatch_cursor_never_reclaims_consumed_arrival(family: str) -> None:
    arrival = _arrival("plan-review-round-advance", sequence=1)
    consumed = consume_plan_round_trigger(offer_plan_round_arrival(empty_plan_round_inbox(), arrival))
    replayed = offer_plan_round_arrival(consumed, arrival)
    assert replayed["current_trigger"] is None
    with pytest.raises((ValueError, Exception), match="current_trigger.value"):
        await _compile_family_join_graph(family).ainvoke(
            {**_join_seed(family), "current_trigger": arrival, "plan_round_inbox": replayed}
        )


@pytest.mark.parametrize("family", _FAMILIES)
async def test_compiled_family_first_arrival_is_exact_current_trigger(family: str) -> None:
    harness = GraphHarness()
    bundle = build_generation_graphs(
        harness.recording_context(owner_id="assurance.generation", contracts=_contracts())
    )
    result = await harness.run(
        _family_graph(bundle, family),
        input=_family_input(family),
        script={
            f"generation.{family}.plan": [committed(_plan(), _RECEIPT), committed(_plan(), _RECEIPT)],
            f"generation.{family}.plan-review": [
                committed(_review("needs_fix", auto_fix=True, used=0), _RECEIPT),
                committed(_review("pass", used=1), _RECEIPT),
            ],
            f"generation.{family}.codegen": [committed(_codegen(family), _RECEIPT)],
        },
    )
    terminal = result.terminal
    assert isinstance(terminal, dict)
    inbox = terminal["plan_round_inbox"]
    current = inbox["current_trigger"]
    assert current is not None
    assert current["predecessor"] == "plan-review-round-advance"
    assert current["business_epoch"] == 0
    assert current["value"] == {"rounds_used": 1, "rounds_budget": 2}
    assert terminal["current_trigger"] == current
    assert terminal["rounds_used"] == current["value"]["rounds_used"]
    assert [call.semantic_node_id for call in result.semantic_calls].count(f"generation.{family}.plan") == 2


@pytest.mark.parametrize("family", _FAMILIES)
async def test_compiled_family_repeated_epochs_preserve_exact_rounds(family: str) -> None:
    harness = GraphHarness()
    bundle = build_generation_graphs(
        harness.recording_context(owner_id="assurance.generation", contracts=_contracts())
    )
    result = await harness.run(
        _family_graph(bundle, family),
        input=_family_input(family),
        script={
            f"generation.{family}.plan": [
                committed(_plan(), _RECEIPT),
                committed(_plan(), _RECEIPT),
                committed(_plan(), _RECEIPT),
            ],
            f"generation.{family}.plan-review": [
                committed(_review("needs_fix", auto_fix=True, used=0), _RECEIPT),
                committed(_review("needs_fix", auto_fix=True, used=1), _RECEIPT),
                committed(_review("pass", used=2), _RECEIPT),
            ],
            f"generation.{family}.codegen": [committed(_codegen(family), _RECEIPT)],
        },
    )
    terminal = result.terminal
    assert isinstance(terminal, dict)
    current = terminal["plan_round_inbox"]["current_trigger"]
    assert current is not None
    assert current["value"] == {"rounds_used": 2, "rounds_budget": 2}
    assert current["value"] != {"rounds_used": 3, "rounds_budget": 2}
    assert current["business_epoch"] == 1
    assert terminal["rounds_used"] == 2
    assert [call.semantic_node_id for call in result.semantic_calls].count(f"generation.{family}.plan") == 3


@pytest.mark.parametrize("family", _FAMILIES)
async def test_last_budgeted_plan_retry_reaches_join(family: str) -> None:
    harness = GraphHarness()
    bundle = build_generation_graphs(
        harness.recording_context(owner_id="assurance.generation", contracts=_contracts())
    )
    result = await harness.run(
        _family_graph(bundle, family),
        input=_family_input(family),
        script={
            f"generation.{family}.plan": [
                committed(_plan(), _RECEIPT),
                committed(_plan(), _RECEIPT),
                committed(_plan(), _RECEIPT),
            ],
            f"generation.{family}.plan-review": [
                committed(_review("needs_fix", auto_fix=True, used=0), _RECEIPT),
                committed(_review("needs_fix", auto_fix=True, used=1), _RECEIPT),
                committed(_review("pass", used=2), _RECEIPT),
            ],
            f"generation.{family}.codegen": [committed(_codegen(family), _RECEIPT)],
        },
    )
    terminal = result.terminal
    assert isinstance(terminal, dict)
    current = terminal["plan_round_inbox"]["current_trigger"]
    assert current is not None
    assert current["value"] == {"rounds_used": 2, "rounds_budget": 2}
    assert current["predecessor"] == "plan-review-round-advance-retry"
    assert terminal["current_trigger"] == current
    assert terminal["rounds_used"] == 2
    assert terminal.get("status") in {"passed", "done"} or terminal.get("decision") == "pass"
    assert [call.semantic_node_id for call in result.semantic_calls].count(f"generation.{family}.plan") == 3
    assert [call.semantic_node_id for call in result.semantic_calls].count(
        f"generation.{family}.plan-review"
    ) == 3
