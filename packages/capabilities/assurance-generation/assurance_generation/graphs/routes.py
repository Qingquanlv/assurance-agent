from __future__ import annotations

from collections.abc import Mapping

from langgraph.types import Send

from assurance_generation.contracts.families import GENERATION_FAMILIES, validate_selected_families
from graph_engine.errors import GraphEngineError
from graph_engine.stategraph.routing import select_exclusive_route


class InsufficientRouteMatches(GraphEngineError):
    """Raised when Generation fanout cannot emit all four family Sends."""


def family_select_named_matches(state: Mapping[str, object], family: str) -> dict[str, str | None]:
    selected = state.get("selected_test_families")
    names = selected if isinstance(selected, list | tuple) else ()
    return {"selected": family if family in names else None}


def _has_budget(state: Mapping[str, object]) -> bool:
    used = state.get("rounds_used", 0)
    budget = state.get("rounds_budget", 0)
    return isinstance(used, int) and isinstance(budget, int) and used < budget


def _within_spent_budget(state: Mapping[str, object]) -> bool:
    used = state.get("rounds_used", 0)
    budget = state.get("rounds_budget", 0)
    return isinstance(used, int) and isinstance(budget, int) and used <= budget


def _is_pass(state: Mapping[str, object]) -> bool:
    return (
        state.get("decision") == "pass"
        and state.get("human_review_required") is not True
        and state.get("codegen_readiness") != "not_ready"
    )


def _is_auto_fix(state: Mapping[str, object]) -> bool:
    return (
        state.get("decision") == "needs_fix"
        and state.get("auto_fix_allowed") is True
        and state.get("human_review_required") is not True
        and _has_budget(state)
    )


def _is_reject(state: Mapping[str, object]) -> bool:
    return state.get("decision") == "reject" and state.get("human_review_required") is not True


def _is_human(state: Mapping[str, object]) -> bool:
    return state.get("decision") == "needs_human_review" or state.get("human_review_required") is True


def plan_review_named_matches(state: Mapping[str, object]) -> dict[str, str | None]:
    return {
        "pass": "codegen" if _is_pass(state) else None,
        "auto_fix": "plan-review-round-advance" if _is_auto_fix(state) else None,
        "reject": "rejected" if _is_reject(state) else None,
        "human": "plan-human-review" if _is_human(state) else None,
    }


def plan_review_retry_named_matches(state: Mapping[str, object]) -> dict[str, str | None]:
    matches = plan_review_named_matches(state)
    return {
        "pass": matches["pass"],
        "auto_fix": "plan-review-round-advance-retry" if matches["auto_fix"] else None,
        "reject": matches["reject"],
        "human": "plan-human-review-retry" if matches["human"] else None,
    }


def plan_human_review_named_matches(state: Mapping[str, object]) -> dict[str, str | None]:
    action = state.get("human_action")
    return {
        "approve": "codegen" if action == "approve" else None,
        "reject": "rejected" if action == "reject" else None,
        "rework": "plan-review-round-advance" if action == "request_rework" and _has_budget(state) else None,
    }


def plan_human_review_retry_named_matches(state: Mapping[str, object]) -> dict[str, str | None]:
    matches = plan_human_review_named_matches(state)
    return {
        "approve": matches["approve"],
        "reject": matches["reject"],
        "rework": "plan-review-round-advance-retry" if matches["rework"] else None,
    }


def plan_advance_named_matches(state: Mapping[str, object]) -> dict[str, str | None]:
    return {"continue": "plan-round-join" if _within_spent_budget(state) else None}


def plan_advance_retry_named_matches(state: Mapping[str, object]) -> dict[str, str | None]:
    return plan_advance_named_matches(state)


def codegen_named_matches(state: Mapping[str, object]) -> dict[str, str | None]:
    verdict = state.get("codegen_verdict")
    needs_fix = state.get("needs_fix")
    accepted = verdict == "accepted" or needs_fix is False
    fix = verdict == "needs_fix" or needs_fix is True
    return {
        "accepted": "done" if accepted and not fix else None,
        "needs_fix": "codegen-round-advance" if fix else None,
    }


def family_entry_named_matches(state: Mapping[str, object]) -> dict[str, str | None]:
    return {"selected": "plan" if state.get("lane_selected") else None}


def route_families(state: Mapping[str, object]) -> list[Send]:
    raw = state.get("selected_test_families")
    try:
        if not isinstance(raw, list | tuple):
            raise ValueError("selected_test_families is missing")
        selected = validate_selected_families(list(raw))
    except (TypeError, ValueError) as error:
        raise InsufficientRouteMatches(str(error)) from error
    destinations = GENERATION_FAMILIES
    if len(destinations) != 4:
        raise InsufficientRouteMatches("generation fanout requires four family destinations")
    sends: list[Send] = []
    for family in destinations:
        sends.append(
            Send(
                family,
                {
                    "change_id": state.get("change_id"),
                    "selected_test_families": list(selected),
                    "capability_leafs": state.get("capability_leafs"),
                    "allowed_artifact_paths": state.get("allowed_artifact_paths"),
                    "family": family,
                    "lane_selected": family in selected,
                    "rounds_used": 0,
                    "rounds_budget": 2,
                    "review_stage": "plan",
                },
            )
        )
    return sends


def route_family_entry(state: Mapping[str, object]) -> str:
    return select_exclusive_route(family_entry_named_matches(state), otherwise="skip")


def route_plan_review(state: Mapping[str, object]) -> str:
    return select_exclusive_route(plan_review_named_matches(state), otherwise="exhausted")


def route_plan_review_retry(state: Mapping[str, object]) -> str:
    return select_exclusive_route(plan_review_retry_named_matches(state), otherwise="exhausted")


def route_plan_human_review(state: Mapping[str, object]) -> str:
    return select_exclusive_route(plan_human_review_named_matches(state), otherwise="exhausted")


def route_plan_human_review_retry(state: Mapping[str, object]) -> str:
    return select_exclusive_route(plan_human_review_retry_named_matches(state), otherwise="exhausted")


def route_plan_advance(state: Mapping[str, object]) -> str:
    return select_exclusive_route(plan_advance_named_matches(state), otherwise="exhausted")


def route_plan_advance_retry(state: Mapping[str, object]) -> str:
    return select_exclusive_route(plan_advance_retry_named_matches(state), otherwise="exhausted")


def route_codegen(state: Mapping[str, object]) -> str:
    return select_exclusive_route(codegen_named_matches(state), otherwise="exhausted")


__all__ = [
    "InsufficientRouteMatches",
    "codegen_named_matches",
    "family_entry_named_matches",
    "family_select_named_matches",
    "plan_advance_named_matches",
    "plan_advance_retry_named_matches",
    "plan_human_review_named_matches",
    "plan_human_review_retry_named_matches",
    "plan_review_named_matches",
    "plan_review_retry_named_matches",
    "route_codegen",
    "route_families",
    "route_family_entry",
    "route_plan_advance",
    "route_plan_advance_retry",
    "route_plan_human_review",
    "route_plan_human_review_retry",
    "route_plan_review",
    "route_plan_review_retry",
]
