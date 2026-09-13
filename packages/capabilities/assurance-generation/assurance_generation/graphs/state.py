from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Literal, NotRequired, TypedDict

from assurance_generation.contracts.families import GENERATION_FAMILIES
from graph_engine.stategraph.checkpoint_bridge import CheckpointBridgeState

PLAN_ROUND_PREDECESSORS = (
    "plan-review-round-advance",
    "plan-review-round-advance-retry",
)

PlanRoundPredecessor = Literal["plan-review-round-advance", "plan-review-round-advance-retry"]


class PlanRoundArrival(TypedDict):
    business_epoch: int
    predecessor: PlanRoundPredecessor
    source_activation: str
    sequence: int
    value: dict[str, int]
    arrival_id: str


class PlanRoundInbox(TypedDict):
    arrivals: list[PlanRoundArrival]
    dispatched_ids: list[str]
    current_trigger: PlanRoundArrival | None


class FamilyLaneResult(TypedDict):
    coverage_epoch: int
    family: str
    receipt_id: str
    selected: bool
    status: str
    generated: NotRequired[dict[str, object]]


def empty_plan_round_inbox() -> PlanRoundInbox:
    return {"arrivals": [], "dispatched_ids": [], "current_trigger": None}


def make_plan_round_arrival(
    *,
    predecessor: str,
    business_epoch: int,
    sequence: int,
    value: Mapping[str, int],
    source_activation: str | None = None,
) -> PlanRoundArrival:
    if predecessor not in PLAN_ROUND_PREDECESSORS:
        raise ValueError(f"unknown plan-round predecessor: {predecessor}")
    arrival_id = f"{predecessor}.{business_epoch}.{sequence}"
    return {
        "business_epoch": business_epoch,
        "predecessor": predecessor,  # type: ignore[typeddict-item]
        "source_activation": source_activation or f"{predecessor}-{business_epoch}-{sequence}",
        "sequence": sequence,
        "value": {
            "rounds_used": int(value["rounds_used"]),
            "rounds_budget": int(value["rounds_budget"]),
        },
        "arrival_id": arrival_id,
    }


def make_family_lane_result(
    *,
    coverage_epoch: int = 0,
    family: str,
    receipt_id: str,
    selected: bool,
    status: str,
) -> FamilyLaneResult:
    return {
        "coverage_epoch": coverage_epoch,
        "family": family,
        "receipt_id": receipt_id,
        "selected": selected,
        "status": status,
    }


def _as_inbox(raw: object) -> PlanRoundInbox:
    if raw is None:
        return empty_plan_round_inbox()
    if not isinstance(raw, Mapping):
        raise TypeError("plan-round inbox must be a mapping")
    arrivals = [item for item in list(raw.get("arrivals") or []) if isinstance(item, Mapping)]
    dispatched = [str(item) for item in list(raw.get("dispatched_ids") or [])]
    current = raw.get("current_trigger")
    return {
        "arrivals": [dict(item) for item in arrivals],  # type: ignore[misc]
        "dispatched_ids": dispatched,
        "current_trigger": dict(current) if isinstance(current, Mapping) else None,  # type: ignore[arg-type]
    }


def _as_int(value: object, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an int")
    return value


def _arrival_sort_key(arrival: Mapping[str, object]) -> tuple[int, int, int, str]:
    predecessor = str(arrival["predecessor"])
    predecessor_order = (
        PLAN_ROUND_PREDECESSORS.index(predecessor) if predecessor in PLAN_ROUND_PREDECESSORS else 99
    )
    return (
        _as_int(arrival["business_epoch"], name="business_epoch"),
        _as_int(arrival["sequence"], name="sequence"),
        predecessor_order,
        str(arrival["arrival_id"]),
    )


def merge_plan_round_inbox(left: object, right: object) -> PlanRoundInbox:
    left_inbox = _as_inbox(left)
    right_inbox = _as_inbox(right)
    by_id: dict[str, PlanRoundArrival] = {}
    for arrival in [*left_inbox["arrivals"], *right_inbox["arrivals"]]:
        by_id[arrival["arrival_id"]] = arrival
    arrivals = sorted(by_id.values(), key=_arrival_sort_key)
    dispatched = sorted(set(left_inbox["dispatched_ids"]) | set(right_inbox["dispatched_ids"]))
    current = next((item for item in arrivals if item["arrival_id"] not in dispatched), None)
    return {"arrivals": arrivals, "dispatched_ids": dispatched, "current_trigger": current}


def offer_plan_round_arrival(inbox: object, arrival: PlanRoundArrival) -> PlanRoundInbox:
    return merge_plan_round_inbox(
        inbox,
        {"arrivals": [arrival], "dispatched_ids": [], "current_trigger": None},
    )


def consume_plan_round_trigger(inbox: object) -> PlanRoundInbox:
    current = _as_inbox(inbox).get("current_trigger")
    if current is None:
        return merge_plan_round_inbox(inbox, empty_plan_round_inbox())
    return merge_plan_round_inbox(
        inbox,
        {
            "arrivals": [],
            "dispatched_ids": [current["arrival_id"]],
            "current_trigger": None,
        },
    )


def _as_results(raw: object) -> list[FamilyLaneResult]:
    if raw is None:
        return []
    if isinstance(raw, Mapping) and "family" in raw:
        return [dict(raw)]  # type: ignore[arg-type]
    if not isinstance(raw, list):
        raise TypeError("family results must be a list")
    return [dict(item) for item in raw if isinstance(item, Mapping)]  # type: ignore[misc]


def merge_family_results(left: object, right: object) -> list[FamilyLaneResult]:
    by_key: dict[tuple[int, str, str], FamilyLaneResult] = {}
    for item in [*_as_results(left), *_as_results(right)]:
        by_key[(int(item.get("coverage_epoch", 0)), str(item["family"]), str(item["receipt_id"]))] = item
    order = {name: index for index, name in enumerate(GENERATION_FAMILIES)}
    return sorted(
        by_key.values(),
        key=lambda item: (
            int(item.get("coverage_epoch", 0)),
            order.get(str(item["family"]), 99),
            str(item["receipt_id"]),
        ),
    )


class FamilyLaneOutput(TypedDict, total=False):
    family_results: Annotated[list[FamilyLaneResult], merge_family_results]


class GenerationState(CheckpointBridgeState, total=False):
    generation_result: dict[str, object]
    generation_receipt: dict[str, object]
    plan_files: list[str]
    codegen_output: dict[str, object]
    codegen_receipt: dict[str, object]
    coverage_epoch: int
    reviewed_case: dict[str, object]
    source_artifacts: list[dict[str, str]]
    change_id: str
    data_knowledge: dict[str, object]
    init_result: dict[str, object]
    plan_digest: str
    plan_ref: dict[str, str]
    selected_test_families: list[str]
    capability_leafs: list[str]
    allowed_artifact_paths: list[str]
    family: str
    lane_selected: bool
    review_stage: str
    rounds_used: int
    rounds_budget: int
    decision: str
    auto_fix_allowed: bool
    human_review_required: bool
    human_action: str
    codegen_readiness: str
    reviewed_plan: dict[str, object]
    artifacts: list[dict[str, object]]
    plan_round_inbox: Annotated[PlanRoundInbox, merge_plan_round_inbox]
    current_trigger: PlanRoundArrival | None
    family_results: Annotated[list[FamilyLaneResult], merge_family_results]
    families: dict[str, dict[str, bool]]
    status: str
    attempt_failure: dict[str, object]


__all__ = [
    "GENERATION_FAMILIES",
    "PLAN_ROUND_PREDECESSORS",
    "FamilyLaneOutput",
    "FamilyLaneResult",
    "GenerationState",
    "PlanRoundArrival",
    "PlanRoundInbox",
    "PlanRoundPredecessor",
    "consume_plan_round_trigger",
    "empty_plan_round_inbox",
    "make_family_lane_result",
    "make_plan_round_arrival",
    "merge_family_results",
    "merge_plan_round_inbox",
    "offer_plan_round_arrival",
]
