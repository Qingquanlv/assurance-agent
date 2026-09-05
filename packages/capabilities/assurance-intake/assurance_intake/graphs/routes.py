from __future__ import annotations

from collections.abc import Mapping

from graph_engine.stategraph.routing import select_exclusive_route


def _has_budget(state: Mapping[str, object]) -> bool:
    used = state.get("rounds_used", 0)
    budget = state.get("rounds_budget", 0)
    return isinstance(used, int) and isinstance(budget, int) and used < budget


def _is_pass(state: Mapping[str, object]) -> bool:
    return state.get("decision") == "pass" and state.get("human_review_required") is not True


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


def case_review_named_matches(state: Mapping[str, object]) -> dict[str, str | None]:
    if state.get("attempt_failure"):
        return {"pass": None, "auto_fix": None, "reject": None, "human": None}
    return {
        "pass": "done" if _is_pass(state) else None,
        "auto_fix": "review-round-advance" if _is_auto_fix(state) else None,
        "reject": "rejected" if _is_reject(state) else None,
        "human": "human-review" if _is_human(state) else None,
    }


def case_review_retry_named_matches(state: Mapping[str, object]) -> dict[str, str | None]:
    matches = case_review_named_matches(state)
    return {
        "pass": matches["pass"],
        "auto_fix": "review-round-advance-retry" if matches["auto_fix"] else None,
        "reject": matches["reject"],
        "human": "human-review-retry" if matches["human"] else None,
    }


def human_review_named_matches(state: Mapping[str, object]) -> dict[str, str | None]:
    action = state.get("human_action")
    return {
        "approve": "done" if action == "approve" else None,
        "reject": "rejected" if action == "reject" else None,
        "rework": "review-round-advance" if action == "request_rework" and _has_budget(state) else None,
    }


def human_review_retry_named_matches(state: Mapping[str, object]) -> dict[str, str | None]:
    matches = human_review_named_matches(state)
    return {
        "approve": matches["approve"],
        "reject": matches["reject"],
        "rework": "review-round-advance-rework-retry" if matches["rework"] else None,
    }


def case_design_named_matches(state: Mapping[str, object]) -> dict[str, str | None]:
    failed = bool(state.get("attempt_failure"))
    return {
        "pass": "done" if not failed and state.get("validation_status") == "pass" else None,
        "failed": "failed" if failed else None,
    }


def route_preparation_attempt(state: Mapping[str, object]) -> str:
    return "failed" if state.get("attempt_failure") else "committed"


def route_case_design_result(state: Mapping[str, object]) -> str:
    return "review" if state.get("status") == "passed" else "failed"


def route_case_design_repair(state: Mapping[str, object]) -> str:
    if state.get("attempt_failure"):
        return "failed"
    return "done" if state.get("validation_status") == "pass" else "failed"


def route_case_review(state: Mapping[str, object]) -> str:
    return select_exclusive_route(case_review_named_matches(state), otherwise="exhausted")


def route_case_review_retry(state: Mapping[str, object]) -> str:
    return select_exclusive_route(case_review_retry_named_matches(state), otherwise="exhausted")


def route_human_review(state: Mapping[str, object]) -> str:
    return select_exclusive_route(human_review_named_matches(state), otherwise="exhausted")


def route_human_review_retry(state: Mapping[str, object]) -> str:
    return select_exclusive_route(human_review_retry_named_matches(state), otherwise="exhausted")


def route_case_design(state: Mapping[str, object]) -> str:
    return select_exclusive_route(case_design_named_matches(state), otherwise="case-design-repair")


__all__ = [
    "case_design_named_matches",
    "case_review_named_matches",
    "case_review_retry_named_matches",
    "human_review_named_matches",
    "human_review_retry_named_matches",
    "route_case_design",
    "route_case_design_repair",
    "route_case_design_result",
    "route_case_review",
    "route_case_review_retry",
    "route_human_review",
    "route_human_review_retry",
    "route_preparation_attempt",
]
