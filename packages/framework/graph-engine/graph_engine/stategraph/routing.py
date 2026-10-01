from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any

from langgraph.graph import StateGraph

from graph_engine.errors import GraphEngineError


class AmbiguousRouteMatch(GraphEngineError):
    """Raised when exclusive routing sees two or more simultaneous named matches."""

    def __init__(self, matches: tuple[str, ...]) -> None:
        self.matches = matches
        super().__init__(f"ambiguous exclusive route matches: {matches}")


def select_exclusive_route(
    named_matches: Mapping[str, str | None],
    *,
    otherwise: str,
) -> str:
    if not otherwise:
        raise ValueError("otherwise must be nonempty")
    matched = {name: target for name, target in named_matches.items() if target}
    if len(matched) == 1:
        return next(iter(matched.values()))
    if not matched:
        return otherwise
    raise AmbiguousRouteMatch(tuple(matched))


ATTEMPT_FAILURE_KEY = "attempt_failure"


def add_attempt_edge(builder: StateGraph[Any], source: str, target: str, *, on_failure: str) -> None:
    def route_attempt(state: Mapping[str, object]) -> str:
        return on_failure if state.get(ATTEMPT_FAILURE_KEY) else target

    builder.add_conditional_edges(source, route_attempt, {target: target, on_failure: on_failure})


def add_route(
    builder: StateGraph[Any],
    source: str,
    route: Callable[[Mapping[str, object]], str],
    *,
    targets: Iterable[str],
    on_failure: str | None = None,
) -> None:
    names = tuple(dict.fromkeys((*targets, *(() if on_failure is None else (on_failure,)))))

    def decide(state: Mapping[str, object]) -> str:
        if on_failure is not None and state.get(ATTEMPT_FAILURE_KEY):
            return on_failure
        target = route(state)
        if target not in names:
            raise ValueError(f"{source!r} routed to undeclared target {target!r}")
        return target

    decide.__name__ = getattr(route, "__name__", "route")
    builder.add_conditional_edges(source, decide, {name: name for name in names})


__all__ = [
    "AmbiguousRouteMatch",
    "add_attempt_edge",
    "add_route",
    "select_exclusive_route",
]
