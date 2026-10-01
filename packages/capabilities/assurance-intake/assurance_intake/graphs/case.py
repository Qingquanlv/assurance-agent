from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Literal, cast

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from graph_engine.boot.boot import CapabilityBuildContext
from graph_engine.plugin_api import FrozenModel
from graph_engine.stategraph import add_attempt_node, add_route, human_gate

from assurance_intake.graphs.calls import (
    activation_case_review,
    publish_case_review,
    select_case_review,
)
from assurance_intake.graphs.routes import (
    route_case_design_result,
    route_case_review,
    route_human_review,
)
from assurance_intake.graphs.state import (
    IntakeState,
    advance_join,
    review_round_advance,
    terminal_exhausted,
    terminal_rejected,
    terminal_reviewed,
)
from assurance_intake.ops.case_review import op as case_review

_CASE_REVIEW_TARGETS = ("done", "review-round-advance", "rejected", "human-review", "exhausted")
_HUMAN_TARGETS = ("done", "rejected", "review-round-advance", "exhausted")


HUMAN_REVIEW_ACTIONS = ("approve", "reject", "request_rework")


class HumanReviewDecision(FrozenModel):
    action: Literal["approve", "reject", "request_rework"]


def _human_review_payload(state: Mapping[str, object]) -> dict[str, object]:
    return {
        "reason": "needs_human_review",
        "actions": list(HUMAN_REVIEW_ACTIONS),
        "rounds_used": state.get("rounds_used", 0),
        "rounds_budget": state.get("rounds_budget", 2),
    }


human_review = human_gate(_human_review_payload, decision=HumanReviewDecision)


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
        contract_id=case_review.contract_id,
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


__all__ = ["HUMAN_REVIEW_ACTIONS", "HumanReviewDecision", "build_case_graph", "human_review"]
