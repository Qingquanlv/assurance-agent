from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Literal, TypedDict

from graph_engine.stategraph.checkpoint_bridge import CheckpointBridgeState

from assurance_intake.contracts.workflow import CaseFlowResultV1
from assurance_intake.domain.history_refs import merge_history_refs
from assurance_intake.domain.review_rounds import advance_review_round

CASE_REVIEW_PREDECESSORS = ("review-round-advance",)

CaseReviewPredecessor = Literal["review-round-advance"]


class CaseReviewArrival(TypedDict):
    business_epoch: int
    predecessor: CaseReviewPredecessor
    source_activation: str
    sequence: int
    value: dict[str, int]
    arrival_id: str


class CaseReviewInbox(TypedDict):
    arrivals: list[CaseReviewArrival]
    dispatched_ids: list[str]
    current_trigger: CaseReviewArrival | None


def empty_case_review_inbox() -> CaseReviewInbox:
    return {"arrivals": [], "dispatched_ids": [], "current_trigger": None}


def make_case_review_arrival(
    *,
    predecessor: str,
    business_epoch: int,
    sequence: int,
    value: Mapping[str, int],
    source_activation: str | None = None,
) -> CaseReviewArrival:
    if predecessor not in CASE_REVIEW_PREDECESSORS:
        raise ValueError(f"unknown case-review predecessor: {predecessor}")
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


def _as_inbox(raw: object) -> CaseReviewInbox:
    if raw is None:
        return empty_case_review_inbox()
    if not isinstance(raw, Mapping):
        raise TypeError("case review inbox must be a mapping")
    arrivals = [item for item in list(raw.get("arrivals") or []) if isinstance(item, Mapping)]
    dispatched = [str(item) for item in list(raw.get("dispatched_ids") or [])]
    current = raw.get("current_trigger")
    return {
        "arrivals": [dict(item) for item in arrivals],  # type: ignore[misc]
        "dispatched_ids": dispatched,
        "current_trigger": dict(current) if isinstance(current, Mapping) else None,  # type: ignore[arg-type]
    }


def as_int(value: object, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an int")
    return value


def _arrival_sort_key(arrival: Mapping[str, object]) -> tuple[int, int, int, str]:
    predecessor = str(arrival["predecessor"])
    predecessor_order = (
        CASE_REVIEW_PREDECESSORS.index(predecessor) if predecessor in CASE_REVIEW_PREDECESSORS else 99
    )
    return (
        as_int(arrival["business_epoch"], name="business_epoch"),
        as_int(arrival["sequence"], name="sequence"),
        predecessor_order,
        str(arrival["arrival_id"]),
    )


def merge_case_review_inbox(left: object, right: object) -> CaseReviewInbox:
    left_inbox = _as_inbox(left)
    right_inbox = _as_inbox(right)
    by_id: dict[str, CaseReviewArrival] = {}
    for arrival in [*left_inbox["arrivals"], *right_inbox["arrivals"]]:
        by_id[arrival["arrival_id"]] = arrival
    arrivals = sorted(by_id.values(), key=_arrival_sort_key)
    dispatched = sorted(set(left_inbox["dispatched_ids"]) | set(right_inbox["dispatched_ids"]))
    current = next((item for item in arrivals if item["arrival_id"] not in dispatched), None)
    return {"arrivals": arrivals, "dispatched_ids": dispatched, "current_trigger": current}


def offer_case_review_arrival(inbox: object, arrival: CaseReviewArrival) -> CaseReviewInbox:
    return merge_case_review_inbox(
        inbox,
        {"arrivals": [arrival], "dispatched_ids": [], "current_trigger": None},
    )


def consume_case_review_trigger(inbox: object) -> CaseReviewInbox:
    current = _as_inbox(inbox).get("current_trigger")
    if current is None:
        return merge_case_review_inbox(inbox, empty_case_review_inbox())
    return merge_case_review_inbox(
        inbox,
        {
            "arrivals": [],
            "dispatched_ids": [current["arrival_id"]],
            "current_trigger": None,
        },
    )


class IntakeState(CheckpointBridgeState, total=False):
    change_id: str
    requirement: str
    candidate_test_families: list[str]
    capability_catalog: dict[str, str]
    product_policy: dict[str, str]
    data_knowledge: dict[str, str]
    budgets: dict[str, int]
    family_policy: dict[str, object]
    selected_test_families: list[str]
    plan_digest: str
    plan_ref: dict[str, str]
    case_delta_paths: list[str]
    capability_leafs: list[str]
    allowed_artifact_paths: list[str]
    rounds_used: int
    rounds_budget: int
    decision: str
    auto_fix_allowed: bool
    human_review_required: bool
    human_action: str
    validation_status: str
    validation_attempt: int
    validation_error: str | None
    artifacts: list[dict[str, object]]
    history_refs: Annotated[list[dict[str, str]], merge_history_refs]
    case_review_inbox: Annotated[CaseReviewInbox, merge_case_review_inbox]
    current_trigger: CaseReviewArrival | None
    status: str
    attempt_failure: dict[str, object]
    coverage_epoch: int
    preparation_refs: list[dict[str, str]]
    case_refs: list[dict[str, str]]
    case_rework_context: dict[str, object] | None
    reviewed_case: dict[str, object]
    case_receipt: dict[str, str]
    receipt: dict[str, str]
    ui_exploration_ref: dict[str, str]
    api_discovery_ref: dict[str, str]


def current_trigger(state: Mapping[str, object]) -> Mapping[str, object] | None:
    inbox = state.get("case_review_inbox")
    if isinstance(inbox, Mapping):
        nested = inbox.get("current_trigger")
        if isinstance(nested, Mapping):
            return nested
    return None


def apply_current_trigger(state: Mapping[str, object]) -> dict[str, object]:
    trigger = current_trigger(state)
    if trigger is None:
        raise ValueError("case-design-retry reads current_trigger.value only")
    value = trigger.get("value")
    if not isinstance(value, Mapping):
        raise ValueError("current trigger value is missing")
    arrival: CaseReviewArrival = {
        "business_epoch": as_int(trigger["business_epoch"], name="business_epoch"),
        "predecessor": trigger["predecessor"],  # type: ignore[typeddict-item]
        "source_activation": str(trigger["source_activation"]),
        "sequence": as_int(trigger["sequence"], name="sequence"),
        "value": {
            "rounds_used": as_int(value["rounds_used"], name="rounds_used"),
            "rounds_budget": as_int(value["rounds_budget"], name="rounds_budget"),
        },
        "arrival_id": str(trigger["arrival_id"]),
    }
    return {
        "current_trigger": arrival,
        "rounds_used": arrival["value"]["rounds_used"],
        "rounds_budget": arrival["value"]["rounds_budget"],
    }


def _next_sequence(inbox: Mapping[str, object]) -> int:
    arrivals = inbox.get("arrivals") or []
    if not isinstance(arrivals, list) or not arrivals:
        return 1
    return (
        max(as_int(item["sequence"], name="sequence") for item in arrivals if isinstance(item, Mapping)) + 1
    )


def offer_advance(state: Mapping[str, object], predecessor: str) -> dict[str, object]:
    inbox = state.get("case_review_inbox") or empty_case_review_inbox()
    if not isinstance(inbox, Mapping):
        inbox = empty_case_review_inbox()
    if inbox.get("current_trigger"):
        inbox = consume_case_review_trigger(inbox)
    used = as_int(state["rounds_used"], name="rounds_used")
    arrival = make_case_review_arrival(
        predecessor=predecessor,
        business_epoch=max(0, used - 1),
        sequence=_next_sequence(inbox),
        value={"rounds_used": used, "rounds_budget": as_int(state["rounds_budget"], name="rounds_budget")},
    )
    merged = offer_case_review_arrival(inbox, arrival)
    return {"case_review_inbox": merged}


def advance_review_round_node(state: Mapping[str, object]) -> dict[str, object]:
    return advance_review_round(
        {"rounds_used": state["rounds_used"], "rounds_budget": state["rounds_budget"]}
    ).model_dump(mode="json")


def review_round_advance(state: Mapping[str, object]) -> dict[str, object]:
    advanced = advance_review_round_node(state)
    return {**advanced, **offer_advance({**dict(state), **advanced}, "review-round-advance")}


def advance_join(state: Mapping[str, object]) -> dict[str, object]:
    return apply_current_trigger(state)


def terminal_done(state: Mapping[str, object]) -> dict[str, object]:
    return {"status": "passed", "decision": state.get("decision", "pass")}


def terminal_reviewed(state: Mapping[str, object]) -> dict[str, object]:
    result = CaseFlowResultV1.model_validate(
        {
            "status": "reviewed",
            "reviewed_case": state.get("reviewed_case"),
            "receipt": state.get("case_receipt"),
        }
    )
    return {**result.model_dump(mode="json"), "decision": "pass"}


def terminal_prepared(state: Mapping[str, object]) -> dict[str, object]:
    del state
    return {"status": "prepared"}


def terminal_failed(state: Mapping[str, object]) -> dict[str, object]:
    del state
    return {"status": "failed"}


def terminal_rejected(state: Mapping[str, object]) -> dict[str, object]:
    return {"status": "rejected", "decision": "reject"}


def terminal_exhausted(state: Mapping[str, object]) -> dict[str, object]:
    return {
        "status": "exhausted",
        "decision": "exhausted",
        "rounds_used": state.get("rounds_used", 0),
    }


__all__ = [
    "CASE_REVIEW_PREDECESSORS",
    "CaseReviewArrival",
    "CaseReviewInbox",
    "CaseReviewPredecessor",
    "IntakeState",
    "advance_join",
    "advance_review_round_node",
    "apply_current_trigger",
    "as_int",
    "consume_case_review_trigger",
    "current_trigger",
    "empty_case_review_inbox",
    "make_case_review_arrival",
    "merge_case_review_inbox",
    "offer_advance",
    "offer_case_review_arrival",
    "review_round_advance",
    "terminal_done",
    "terminal_exhausted",
    "terminal_failed",
    "terminal_prepared",
    "terminal_rejected",
    "terminal_reviewed",
]
