from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from assurance_intake.contracts.workflow import CaseFlowResultV1
from assurance_intake.graphs.calls import _as_int, _trigger
from assurance_intake.graphs.state import (
    CaseReviewArrival,
    consume_case_review_trigger,
    empty_case_review_inbox,
    make_case_review_arrival,
    offer_case_review_arrival,
)
from assurance_intake.operations.workflow_state import advance_review_round
from graph_engine.plugin_api import FrozenModel
from graph_engine.stategraph import human_gate

HUMAN_REVIEW_ACTIONS = ("approve", "reject", "request_rework")


class HumanReviewDecision(FrozenModel):
    action: Literal["approve", "reject", "request_rework"]


def apply_current_trigger(state: Mapping[str, object]) -> dict[str, object]:
    trigger = _trigger(state)
    if trigger is None:
        raise ValueError("case-design-retry reads current_trigger.value only")
    value = trigger.get("value")
    if not isinstance(value, Mapping):
        raise ValueError("current trigger value is missing")
    arrival: CaseReviewArrival = {
        "business_epoch": _as_int(trigger["business_epoch"], name="business_epoch"),
        "predecessor": trigger["predecessor"],  # type: ignore[typeddict-item]
        "source_activation": str(trigger["source_activation"]),
        "sequence": _as_int(trigger["sequence"], name="sequence"),
        "value": {
            "rounds_used": _as_int(value["rounds_used"], name="rounds_used"),
            "rounds_budget": _as_int(value["rounds_budget"], name="rounds_budget"),
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
        max(_as_int(item["sequence"], name="sequence") for item in arrivals if isinstance(item, Mapping)) + 1
    )


def offer_advance(state: Mapping[str, object], predecessor: str) -> dict[str, object]:
    inbox = state.get("case_review_inbox") or empty_case_review_inbox()
    if not isinstance(inbox, Mapping):
        inbox = empty_case_review_inbox()
    if inbox.get("current_trigger"):
        inbox = consume_case_review_trigger(inbox)
    used = _as_int(state["rounds_used"], name="rounds_used")
    arrival = make_case_review_arrival(
        predecessor=predecessor,
        business_epoch=max(0, used - 1),
        sequence=_next_sequence(inbox),
        value={"rounds_used": used, "rounds_budget": _as_int(state["rounds_budget"], name="rounds_budget")},
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


def _human_review_payload(state: Mapping[str, object]) -> dict[str, object]:
    return {
        "reason": "needs_human_review",
        "actions": list(HUMAN_REVIEW_ACTIONS),
        "rounds_used": state.get("rounds_used", 0),
        "rounds_budget": state.get("rounds_budget", 2),
    }


human_review = human_gate(_human_review_payload, decision=HumanReviewDecision)


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
    "HUMAN_REVIEW_ACTIONS",
    "HumanReviewDecision",
    "advance_join",
    "advance_review_round_node",
    "apply_current_trigger",
    "human_review",
    "review_round_advance",
    "terminal_failed",
    "terminal_done",
    "terminal_exhausted",
    "terminal_prepared",
    "terminal_rejected",
    "terminal_reviewed",
]
