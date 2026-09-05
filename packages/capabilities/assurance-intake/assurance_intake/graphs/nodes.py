from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from langgraph.types import interrupt
from pydantic import BaseModel

from assurance_intake.contracts.agent import (
    CaseDesignInputV1,
    CaseReviewInputV1,
    ExploreInputV1,
    IntakeInputV1,
)
from assurance_intake.contracts.decisions import advance_review_round
from assurance_intake.graphs.state import (
    CaseReviewArrival,
    consume_case_review_trigger,
    empty_case_review_inbox,
    make_case_review_arrival,
    offer_case_review_arrival,
)
from graph_engine.attempts.keys import BusinessActivation
from graph_engine.plugin_api import FrozenModel

HUMAN_REVIEW_ACTIONS = ("approve", "reject", "request_rework")
activation_one_shot = BusinessActivation.one_shot()


class HumanReviewDecision(FrozenModel):
    action: Literal["approve", "reject", "request_rework"]


def _skill_payload(state: Mapping[str, object]) -> dict[str, object]:
    return {
        "change_id": state["change_id"],
        "capability_leafs": state["capability_leafs"],
        "artifact_paths": state["allowed_artifact_paths"],
    }


def select_intake(state: Mapping[str, object]) -> IntakeInputV1:
    return IntakeInputV1.model_validate({**_skill_payload(state), "requirement": state["requirement"]})


def select_explore(state: Mapping[str, object]) -> ExploreInputV1:
    return ExploreInputV1.model_validate(_skill_payload(state))


def select_case_design(state: Mapping[str, object]) -> CaseDesignInputV1:
    return CaseDesignInputV1.model_validate(
        {
            **_skill_payload(state),
            "selected_test_families": state["selected_test_families"],
            "case_delta_paths": state["case_delta_paths"],
            "validation_attempt": 0,
        }
    )


def select_case_design_retry(state: Mapping[str, object]) -> CaseDesignInputV1:
    return select_case_design(state)


def select_case_design_repair(state: Mapping[str, object]) -> CaseDesignInputV1:
    return CaseDesignInputV1.model_validate(
        {
            **_skill_payload(state),
            "selected_test_families": state["selected_test_families"],
            "case_delta_paths": state["case_delta_paths"],
            "validation_attempt": 1,
            "validation_error": state["validation_error"],
        }
    )


def select_case_review(state: Mapping[str, object]) -> CaseReviewInputV1:
    return CaseReviewInputV1.model_validate(
        {**_skill_payload(state), "case_delta_paths": state["case_delta_paths"]}
    )


def _trigger(state: Mapping[str, object]) -> Mapping[str, object] | None:
    inbox = state.get("case_review_inbox")
    if isinstance(inbox, Mapping):
        nested = inbox.get("current_trigger")
        if isinstance(nested, Mapping):
            return nested
    return None


def activation_case_design(state: Mapping[str, object]) -> BusinessActivation:
    trigger = _trigger(state)
    arrival_id = trigger.get("arrival_id") if trigger is not None else None
    if isinstance(arrival_id, str) and arrival_id:
        return BusinessActivation.for_trigger(arrival_id)
    return BusinessActivation.one_shot()


def activation_case_design_repair(state: Mapping[str, object]) -> BusinessActivation:
    trigger = _trigger(state)
    arrival_id = trigger.get("arrival_id") if trigger is not None else None
    if isinstance(arrival_id, str) and arrival_id:
        return BusinessActivation.for_trigger(f"{arrival_id}.repair")
    return BusinessActivation.for_round(1)


def activation_case_review(state: Mapping[str, object]) -> BusinessActivation:
    trigger = _trigger(state)
    arrival_id = trigger.get("arrival_id") if trigger is not None else None
    if isinstance(arrival_id, str) and arrival_id:
        return BusinessActivation.for_trigger(f"{arrival_id}.review")
    used = state.get("rounds_used", 0)
    return BusinessActivation.for_round(int(used) if isinstance(used, int) else 0)


def _output_payload(output: object) -> dict[str, object]:
    if isinstance(output, BaseModel):
        return output.model_dump(mode="json")
    if isinstance(output, Mapping):
        return {str(name): value for name, value in output.items()}
    raise TypeError("attempt output must be a mapping")


def publish_artifacts(state: Mapping[str, object], output: object, receipt: object) -> dict[str, object]:
    del receipt
    payload = _output_payload(output)
    artifacts = payload.get("artifacts")
    files = payload.get("output_files")
    if artifacts is None and isinstance(files, list):
        artifacts = [{"path": path} for path in files]
    return {
        "artifacts": artifacts or [],
        "decision": payload.get("decision", "pass"),
        "rounds_used": state.get("rounds_used", 0),
        "rounds_budget": state.get("rounds_budget", 2),
    }


def publish_case_design(state: Mapping[str, object], output: object, receipt: object) -> dict[str, object]:
    del receipt
    payload = _output_payload(output)
    return {
        "validation_status": payload.get("validation_status", "pass"),
        "validation_attempt": payload.get("validation_attempt", 0),
        "validation_error": payload.get("validation_error"),
        "artifacts": payload.get("artifacts") or [],
        "rounds_used": state.get("rounds_used", 0),
        "rounds_budget": state.get("rounds_budget", 2),
    }


def _published_int(payload: Mapping[str, object], key: str, fallback: object) -> object:
    value = payload.get(key, fallback)
    if isinstance(value, bool) or not isinstance(value, int):
        return fallback
    return value


def publish_case_review(state: Mapping[str, object], output: object, receipt: object) -> dict[str, object]:
    del receipt
    payload = _output_payload(output)
    return {
        "decision": payload["decision"],
        "auto_fix_allowed": bool(payload.get("auto_fix_allowed", False)),
        "human_review_required": bool(payload.get("human_review_required", False)),
        "artifacts": payload.get("artifacts") or [],
        "rounds_used": _published_int(payload, "rounds_used", state.get("rounds_used", 0)),
        "rounds_budget": _published_int(payload, "rounds_budget", state.get("rounds_budget", 2)),
    }


def _as_int(value: object, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an int")
    return value


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


def review_round_advance_retry(state: Mapping[str, object]) -> dict[str, object]:
    advanced = advance_review_round_node(state)
    return {**advanced, **offer_advance({**dict(state), **advanced}, "review-round-advance-retry")}


def review_round_advance_rework_retry(state: Mapping[str, object]) -> dict[str, object]:
    advanced = advance_review_round_node(state)
    return {**advanced, **offer_advance({**dict(state), **advanced}, "review-round-advance-rework-retry")}


def advance_join(state: Mapping[str, object]) -> dict[str, object]:
    return apply_current_trigger(state)


def _coerce_human_decision(raw: object) -> HumanReviewDecision:
    if isinstance(raw, str):
        return HumanReviewDecision(action=raw)  # type: ignore[arg-type]
    if isinstance(raw, Mapping):
        action = raw.get("action", raw.get("decision"))
        return HumanReviewDecision.model_validate({"action": action})
    return HumanReviewDecision.model_validate(raw)


def human_review(state: Mapping[str, object]) -> dict[str, object]:
    raw = interrupt(
        {
            "reason": "needs_human_review",
            "actions": list(HUMAN_REVIEW_ACTIONS),
            "rounds_used": state.get("rounds_used", 0),
            "rounds_budget": state.get("rounds_budget", 2),
        }
    )
    decision = _coerce_human_decision(raw)
    return {"human_action": decision.action}


def human_review_retry(state: Mapping[str, object]) -> dict[str, object]:
    return human_review(state)


def terminal_done(state: Mapping[str, object]) -> dict[str, object]:
    return {"status": "passed", "decision": state.get("decision", "pass")}


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
    "activation_case_design",
    "activation_case_design_repair",
    "activation_case_review",
    "activation_one_shot",
    "advance_join",
    "advance_review_round_node",
    "apply_current_trigger",
    "human_review",
    "human_review_retry",
    "publish_artifacts",
    "publish_case_design",
    "publish_case_review",
    "review_round_advance",
    "review_round_advance_retry",
    "review_round_advance_rework_retry",
    "select_case_design",
    "select_case_design_repair",
    "select_case_design_retry",
    "select_case_review",
    "select_explore",
    "select_intake",
    "terminal_failed",
    "terminal_done",
    "terminal_exhausted",
    "terminal_prepared",
    "terminal_rejected",
]
