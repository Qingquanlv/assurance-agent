from __future__ import annotations

from typing import TypedDict

import pytest
from langgraph.graph import END, START, StateGraph

from graph_engine.stategraph.routing import AmbiguousRouteMatch, add_route, route_on, select_exclusive_route


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


def test_route_on_returns_a_nonempty_channel_or_otherwise() -> None:
    route = route_on("status", otherwise="failed")
    assert route({"status": "passed"}) == "passed"
    assert route({"status": ""}) == "failed"
    assert route({}) == "failed"
    assert route({"status": None}) == "failed"
    assert route({"status": 1}) == "failed"
    with pytest.raises(ValueError, match="otherwise"):
        route_on("status", otherwise="")
    with pytest.raises(ValueError, match="channel"):
        route_on("", otherwise="failed")


def test_route_on_drives_add_route_and_conditional_edges() -> None:
    class _State(TypedDict, total=False):
        outcome: str
        status: str
        attempt_failure: dict[str, object]
        visited: str

    def land(name: str):
        def node(state: _State) -> dict[str, str]:
            del state
            return {"visited": name}

        return node

    routed: StateGraph[_State] = StateGraph(_State)
    routed.add_node("choose", lambda state: state)
    routed.add_node("pass", land("pass"))
    routed.add_node("fail", land("fail"))
    routed.add_edge(START, "choose")
    add_route(
        routed,
        "choose",
        route_on("outcome", otherwise="fail"),
        targets=("pass", "fail"),
        on_failure="fail",
    )
    routed.add_edge("pass", END)
    routed.add_edge("fail", END)
    graph = routed.compile()
    assert graph.invoke({"outcome": "pass"})["visited"] == "pass"
    assert graph.invoke({})["visited"] == "fail"
    assert graph.invoke({"outcome": "pass", "attempt_failure": {"kind": "x"}})["visited"] == "fail"
    with pytest.raises(ValueError, match="undeclared target 'other'"):
        graph.invoke({"outcome": "other"})

    plain: StateGraph[_State] = StateGraph(_State)
    plain.add_node("choose", lambda state: state)
    plain.add_node("passed", land("passed"))
    plain.add_node("failed", land("failed"))
    plain.add_edge(START, "choose")
    plain.add_conditional_edges(
        "choose",
        route_on("status", otherwise="failed"),
        {"passed": "passed", "failed": "failed"},
    )
    plain.add_edge("passed", END)
    plain.add_edge("failed", END)
    compiled = plain.compile()
    assert compiled.invoke({"status": "passed"})["visited"] == "passed"
    assert compiled.invoke({})["visited"] == "failed"
