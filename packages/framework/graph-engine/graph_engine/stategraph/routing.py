from __future__ import annotations

from collections.abc import Mapping

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


__all__ = ["AmbiguousRouteMatch", "select_exclusive_route"]
