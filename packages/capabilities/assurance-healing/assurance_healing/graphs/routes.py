from __future__ import annotations

from collections.abc import Mapping

from graph_engine.stategraph.routing import select_exclusive_route

from assurance_healing.contracts.coverage_repair import HEALING_REPAIR_OUTCOMES

_ADMIT_OTHERWISE = "not-eligible"
_STATUS_OTHERWISE = "failed"
_FAILURE_ELIGIBLE = frozenset({"test", "test-data"})
_STATUS_TARGETS: dict[str, str] = {
    "repaired": "done",
    "needs_review": "needs-review",
    "exhausted": "exhausted",
    "not_eligible": "not-eligible",
    "failed": "failed",
}


def _as_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _exhausted(state: Mapping[str, object]) -> bool:
    used = _as_int(state.get("rounds_used"))
    budget = _as_int(state.get("rounds_budget"))
    if used is None or budget is None:
        return False
    return used >= budget


def _failure_eligible(state: Mapping[str, object]) -> bool:
    used = _as_int(state.get("rounds_used"))
    budget = _as_int(state.get("rounds_budget"))
    if used is None or budget is None:
        return False
    return (
        state.get("classification") in _FAILURE_ELIGIBLE
        and state.get("fix_eligible") is True
        and used < budget
    )


def _coverage_eligible(state: Mapping[str, object]) -> bool:
    used = _as_int(state.get("rounds_used"))
    budget = _as_int(state.get("rounds_budget"))
    if used is None or budget is None:
        return False
    return state.get("fix_eligible") is True and used < budget


def admit_failure_named_matches(state: Mapping[str, object]) -> dict[str, str | None]:
    return {
        "advance": "repair-round-advance" if _failure_eligible(state) else None,
        "exhausted": "exhausted" if _exhausted(state) else None,
    }


def admit_coverage_named_matches(state: Mapping[str, object]) -> dict[str, str | None]:
    return {
        "advance": "repair-round-advance" if _coverage_eligible(state) else None,
        "exhausted": "exhausted" if _exhausted(state) else None,
    }


def coverage_status_named_matches(state: Mapping[str, object]) -> dict[str, str | None]:
    current = state.get("status")
    return {name: _STATUS_TARGETS[name] if current == name else None for name in HEALING_REPAIR_OUTCOMES}


def route_admit_failure(state: Mapping[str, object]) -> str:
    return select_exclusive_route(admit_failure_named_matches(state), otherwise=_ADMIT_OTHERWISE)


def route_admit_coverage(state: Mapping[str, object]) -> str:
    return select_exclusive_route(admit_coverage_named_matches(state), otherwise=_ADMIT_OTHERWISE)


def failure_status_named_matches(state: Mapping[str, object]) -> dict[str, str | None]:
    current = state.get("status")
    return {
        "applied": "done" if current == "applied" else None,
        "needs_review": "needs-review" if current == "needs_review" else None,
        "not_eligible": "not-eligible" if current == "not_eligible" else None,
        "exhausted": "exhausted" if current == "exhausted" else None,
        "failed": "failed" if current == "failed" else None,
    }


def route_proposal_status(state: Mapping[str, object]) -> str:
    return "failed" if state.get("attempt_failure") else "approval"


def route_proposal_approval(state: Mapping[str, object]) -> str:
    approved = state.get("human_action") == "approve" and isinstance(state.get("approval_ref"), Mapping)
    return select_exclusive_route(
        {
            "apply": "healing.apply-test-repair" if approved else None,
            "review": "needs-review" if not approved else None,
        },
        otherwise="needs-review",
    )


def route_coverage_status(state: Mapping[str, object]) -> str:
    if state.get("attempt_failure"):
        return _STATUS_OTHERWISE
    return select_exclusive_route(coverage_status_named_matches(state), otherwise=_STATUS_OTHERWISE)


def route_failure_status(state: Mapping[str, object]) -> str:
    if state.get("attempt_failure"):
        failure = state.get("attempt_failure")
        if isinstance(failure, Mapping) and failure.get("kind") == "invalid_output":
            return "needs-review"
        return _STATUS_OTHERWISE
    return select_exclusive_route(failure_status_named_matches(state), otherwise=_STATUS_OTHERWISE)


__all__ = [
    "admit_coverage_named_matches",
    "admit_failure_named_matches",
    "coverage_status_named_matches",
    "failure_status_named_matches",
    "route_admit_coverage",
    "route_admit_failure",
    "route_coverage_status",
    "route_failure_status",
    "route_proposal_approval",
    "route_proposal_status",
]
