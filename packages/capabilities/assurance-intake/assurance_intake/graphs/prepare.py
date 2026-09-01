from __future__ import annotations

from collections.abc import Callable, Hashable
from typing import Any, cast

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from assurance_intake.graphs.nodes import (
    advance_join,
    human_review,
    human_review_retry,
    review_round_advance,
    review_round_advance_retry,
    review_round_advance_rework_retry,
    terminal_done,
    terminal_exhausted,
    terminal_rejected,
)
from assurance_intake.graphs.routes import (
    route_case_review,
    route_case_review_retry,
    route_human_review,
    route_human_review_retry,
)
from assurance_intake.graphs.state import IntakeState
from graph_engine.boot.boot import CapabilityBuildContext

_CASE_REVIEW_PATHS: dict[Hashable, str] = {
    "done": "done",
    "review-round-advance": "review-round-advance",
    "rejected": "rejected",
    "human-review": "human-review",
    "exhausted": "exhausted",
}
_CASE_REVIEW_RETRY_PATHS: dict[Hashable, str] = {
    "done": "done",
    "review-round-advance-retry": "review-round-advance-retry",
    "rejected": "rejected",
    "human-review-retry": "human-review-retry",
    "exhausted": "exhausted",
}
_HUMAN_PATHS: dict[Hashable, str] = {
    "done": "done",
    "rejected": "rejected",
    "review-round-advance": "review-round-advance",
    "exhausted": "exhausted",
}
_HUMAN_RETRY_PATHS: dict[Hashable, str] = {
    "done": "done",
    "rejected": "rejected",
    "review-round-advance-rework-retry": "review-round-advance-rework-retry",
    "exhausted": "exhausted",
}


def _node(fn: object) -> Callable[..., Any]:
    return cast(Callable[..., Any], fn)


def build_prepare_graph(
    context: CapabilityBuildContext,
    *,
    intake: CompiledStateGraph,
    explore: CompiledStateGraph,
    case_design: CompiledStateGraph,
    case_review: CompiledStateGraph,
) -> CompiledStateGraph:
    builder: StateGraph[IntakeState] = StateGraph(IntakeState)
    builder.add_node("intake", intake)
    builder.add_node("explore", explore)
    builder.add_node("case-design", case_design)
    builder.add_node("case-review", case_review)
    builder.add_node("case-design-retry", case_design)
    builder.add_node("case-review-retry", case_review)
    builder.add_node("review-round-advance", _node(review_round_advance))
    builder.add_node("review-round-advance-retry", _node(review_round_advance_retry))
    builder.add_node("review-round-advance-rework-retry", _node(review_round_advance_rework_retry))
    builder.add_node("advance-join", _node(advance_join))
    builder.add_node("human-review", _node(human_review))
    builder.add_node("human-review-retry", _node(human_review_retry))
    builder.add_node("done", _node(terminal_done))
    builder.add_node("rejected", _node(terminal_rejected))
    builder.add_node("exhausted", _node(terminal_exhausted))
    builder.add_edge(START, "intake")
    builder.add_edge("intake", "explore")
    builder.add_edge("explore", "case-design")
    builder.add_edge("case-design", "case-review")
    builder.add_conditional_edges(
        "case-review",
        cast(Callable[..., Any], route_case_review),
        _CASE_REVIEW_PATHS,
    )
    builder.add_conditional_edges(
        "human-review",
        cast(Callable[..., Any], route_human_review),
        _HUMAN_PATHS,
    )
    builder.add_edge("review-round-advance", "advance-join")
    builder.add_edge("review-round-advance-retry", "advance-join")
    builder.add_edge("review-round-advance-rework-retry", "advance-join")
    builder.add_edge("advance-join", "case-design-retry")
    builder.add_edge("case-design-retry", "case-review-retry")
    builder.add_conditional_edges(
        "case-review-retry",
        cast(Callable[..., Any], route_case_review_retry),
        _CASE_REVIEW_RETRY_PATHS,
    )
    builder.add_conditional_edges(
        "human-review-retry",
        cast(Callable[..., Any], route_human_review_retry),
        _HUMAN_RETRY_PATHS,
    )
    builder.add_edge("done", END)
    builder.add_edge("rejected", END)
    builder.add_edge("exhausted", END)
    return context.compile_subgraph(builder)


__all__ = ["build_prepare_graph"]
