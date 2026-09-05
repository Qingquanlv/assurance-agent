from __future__ import annotations

from collections.abc import Mapping

from graph_engine.stategraph.routing import select_exclusive_route

from assurance_quality.contracts.decisions import FIX_ELIGIBLE_CLASSIFICATIONS

_COVERAGE_OTHERWISE = "failed"
_FAILURE_OTHERWISE = "failed"


def coverage_named_matches(state: Mapping[str, object]) -> dict[str, str | None]:
    dispositions = (
        "satisfied",
        "coverage_insufficient",
        "repairable_execution_failure",
        "needs_human",
        "blocked",
    )
    raw = state.get("inspection_outcome")
    current = raw.get("disposition") if isinstance(raw, Mapping) else getattr(raw, "disposition", None)
    return {name: name if current == name else None for name in dispositions}


def failure_named_matches(state: Mapping[str, object]) -> dict[str, str | None]:
    classification = state.get("classification")
    eligible = state.get("fix_eligible") is True
    return {
        "fix_eligible": (
            "fix-eligible" if classification in FIX_ELIGIBLE_CLASSIFICATIONS and eligible else None
        ),
        "product_bug": "report-issue" if classification == "product_bug" else None,
        "environment_failure": "report-issue" if classification == "environment_failure" else None,
        "infrastructure_failure": "report-issue" if classification == "infrastructure_failure" else None,
    }


def route_coverage(state: Mapping[str, object]) -> str:
    if state.get("attempt_failure"):
        return _COVERAGE_OTHERWISE
    return select_exclusive_route(coverage_named_matches(state), otherwise=_COVERAGE_OTHERWISE)


def route_attempt(state: Mapping[str, object]) -> str:
    return "failed" if state.get("attempt_failure") else "ready"


def route_failure(state: Mapping[str, object]) -> str:
    if state.get("attempt_failure"):
        return _FAILURE_OTHERWISE
    return select_exclusive_route(failure_named_matches(state), otherwise=_FAILURE_OTHERWISE)


__all__ = [
    "coverage_named_matches",
    "failure_named_matches",
    "route_coverage",
    "route_attempt",
    "route_failure",
]
