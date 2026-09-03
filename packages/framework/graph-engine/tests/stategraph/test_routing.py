from __future__ import annotations

import pytest

from graph_engine.stategraph.routing import AmbiguousRouteMatch, select_exclusive_route


def test_select_exclusive_route_returns_the_single_named_target() -> None:
    assert select_exclusive_route({"ready": "execute", "blocked": None}, otherwise="skip") == "execute"


def test_select_exclusive_route_returns_nonempty_otherwise_when_no_match() -> None:
    assert select_exclusive_route({"ready": None}, otherwise="skip") == "skip"
    assert select_exclusive_route({}, otherwise="exhausted") == "exhausted"


def test_select_exclusive_route_rejects_empty_otherwise() -> None:
    with pytest.raises(ValueError, match="otherwise"):
        select_exclusive_route({}, otherwise="")


def test_select_exclusive_route_raises_on_two_simultaneous_matches() -> None:
    with pytest.raises(AmbiguousRouteMatch) as raised:
        select_exclusive_route({"pass": "codegen", "rework": "plan"}, otherwise="exhausted")
    assert set(raised.value.matches) == {"pass", "rework"}
