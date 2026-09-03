from __future__ import annotations

from collections.abc import Mapping

from graph_engine.stategraph.routing import select_exclusive_route

_FAILED = "failed"


def route_committed(state: Mapping[str, object]) -> str:
    if state.get("attempt_failure"):
        return _FAILED
    return select_exclusive_route({"done": "done"}, otherwise=_FAILED)


def route_auto_review(state: Mapping[str, object]) -> str:
    if state.get("attempt_failure"):
        return _FAILED
    current = state.get("lifecycle_state")
    return select_exclusive_route(
        {
            "approved": "evaluate" if current == "approved" else None,
            "needs_rework": "rework" if current == "needs_rework" else None,
            "rejected": "rejected" if current == "rejected" else None,
            "proposed": "human-review" if current == "proposed" else None,
        },
        otherwise=_FAILED,
    )


def route_human_action(state: Mapping[str, object]) -> str:
    action = state.get("human_action")
    return select_exclusive_route(
        {
            "review": "apply-human-review"
            if action in {"approve", "reject", "request_rework", "supersede"}
            else None
        },
        otherwise=_FAILED,
    )


def route_human_review_result(state: Mapping[str, object]) -> str:
    if state.get("attempt_failure"):
        return _FAILED
    current = state.get("lifecycle_state")
    return select_exclusive_route(
        {
            "approved": "evaluate" if current == "approved" else None,
            "rejected": "rejected" if current == "rejected" else None,
            "needs_rework": "rework" if current == "needs_rework" else None,
            "superseded": "superseded" if current == "superseded" else None,
        },
        otherwise=_FAILED,
    )


def route_apply_evaluate(state: Mapping[str, object]) -> str:
    if state.get("attempt_failure"):
        return _FAILED
    return select_exclusive_route(
        {"passed": "apply" if state.get("outcome") == "passed" else None},
        otherwise=_FAILED,
    )


__all__ = [
    "route_apply_evaluate",
    "route_auto_review",
    "route_committed",
    "route_human_action",
    "route_human_review_result",
]
