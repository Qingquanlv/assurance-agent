from __future__ import annotations

from collections.abc import Callable
from typing import Literal, TypedDict

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command
from pydantic import BaseModel

from graph_engine.stategraph import add_attempt_edge, add_route, coerce_action, human_gate


class _State(TypedDict, total=False):
    attempt_failure: dict[str, object] | None
    pick: str
    visited: list[str]
    human_action: str


class _Decision(BaseModel):
    action: Literal["approve", "reject"]


def _mark(name: str) -> Callable[[_State], dict[str, object]]:
    def node(state: _State) -> dict[str, object]:
        return {"visited": [*state.get("visited", []), name]}

    return node


def _graph(wire: Callable[[StateGraph[_State]], None]) -> StateGraph[_State]:
    builder: StateGraph[_State] = StateGraph(_State)
    for name in ("work", "next", "other", "failed"):
        builder.add_node(name, _mark(name))
    builder.add_edge(START, "work")
    wire(builder)
    for name in ("next", "other", "failed"):
        builder.add_edge(name, END)
    return builder


def test_attempt_edge_follows_commit_or_failure() -> None:
    graph = _graph(lambda b: add_attempt_edge(b, "work", "next", on_failure="failed")).compile()
    assert graph.invoke({})["visited"] == ["work", "next"]
    assert graph.invoke({"attempt_failure": {"kind": "x"}})["visited"] == ["work", "failed"]


def test_route_checks_failure_first_and_rejects_undeclared_targets() -> None:
    def pick(state: _State) -> str:
        return state["pick"]

    graph = _graph(
        lambda b: add_route(b, "work", pick, targets=("next", "other"), on_failure="failed")
    ).compile()
    assert graph.invoke({"pick": "other"})["visited"] == ["work", "other"]
    assert graph.invoke({"pick": "other", "attempt_failure": {"k": 1}})["visited"] == ["work", "failed"]
    with pytest.raises(ValueError, match="undeclared"):
        graph.invoke({"pick": "nowhere"})


@pytest.mark.parametrize("raw", ["approve", {"action": "approve"}, {"decision": "approve"}])
def test_coerce_action_accepts_resume_shapes(raw: object) -> None:
    assert coerce_action(_Decision, raw).action == "approve"


def test_human_gate_interrupts_then_writes_action() -> None:
    builder: StateGraph[_State] = StateGraph(_State)
    builder.add_node("review", human_gate(lambda _s: {"reason": "r"}, decision=_Decision))
    builder.add_edge(START, "review")
    builder.add_edge("review", END)
    graph = builder.compile(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "t"}}
    first = graph.invoke({}, config)
    assert first["__interrupt__"][0].value == {"reason": "r"}
    assert graph.invoke(Command(resume={"decision": "approve"}), config)["human_action"] == "approve"
