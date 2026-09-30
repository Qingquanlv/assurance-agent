from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from assurance_intake.graphs.nodes import (
    activation_case_review,
    advance_join,
    human_review,
    publish_case_review,
    review_round_advance,
    select_case_review,
    terminal_exhausted,
    terminal_rejected,
    terminal_reviewed,
)
from assurance_intake.graphs.routes import (
    route_case_design_result,
    route_case_review,
    route_human_review,
)
from assurance_intake.graphs.state import IntakeState
from graph_engine.boot.boot import CapabilityBuildContext
from graph_engine.stategraph import add_attempt_node, add_route

CASE_REVIEW_ID = "assurance.intake.agent.case-review.v1"
_CASE_REVIEW_TARGETS = ("done", "review-round-advance", "rejected", "human-review", "exhausted")
_HUMAN_TARGETS = ("done", "rejected", "review-round-advance", "exhausted")


def _node(fn: object) -> Callable[..., Any]:
    return cast(Callable[..., Any], fn)


def build_case_graph(
    context: CapabilityBuildContext,
    *,
    case_design: CompiledStateGraph,
) -> CompiledStateGraph:
    builder: StateGraph[IntakeState] = StateGraph(IntakeState)
    builder.add_node("case-design", case_design)
    add_attempt_node(
        builder,
        context,
        "case-review",
        semantic_node_id="intake.case-review",
        contract_id=CASE_REVIEW_ID,
        activation=activation_case_review,
        select=select_case_review,
        publish=publish_case_review,
    )
    builder.add_node("review-round-advance", _node(review_round_advance))
    builder.add_node("advance-join", _node(advance_join))
    builder.add_node("human-review", _node(human_review))
    builder.add_node("done", _node(terminal_reviewed))
    builder.add_node("rejected", _node(terminal_rejected))
    builder.add_node("exhausted", _node(terminal_exhausted))
    builder.add_edge(START, "case-design")
    add_route(builder, "case-design", route_case_design_result, targets=("case-review", "exhausted"))
    add_route(builder, "case-review", route_case_review, targets=_CASE_REVIEW_TARGETS)
    add_route(builder, "human-review", route_human_review, targets=_HUMAN_TARGETS)
    builder.add_edge("review-round-advance", "advance-join")
    builder.add_edge("advance-join", "case-design")
    for terminal in ("done", "rejected", "exhausted"):
        builder.add_edge(terminal, END)
    return context.compile_subgraph(builder)


__all__ = ["build_case_graph"]
