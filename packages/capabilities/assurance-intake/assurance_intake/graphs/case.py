from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from assurance_intake.graphs.nodes import terminal_done
from assurance_intake.graphs.state import IntakeState
from graph_engine.boot.boot import CapabilityBuildContext


def build_case_graph(
    context: CapabilityBuildContext,
    *,
    case_design: CompiledStateGraph,
    case_review: CompiledStateGraph,
) -> CompiledStateGraph:
    builder: StateGraph[IntakeState] = StateGraph(IntakeState)
    builder.add_node("case-design", case_design)
    builder.add_node("case-review", case_review)
    builder.add_node("done", cast(Callable[..., Any], terminal_done))
    builder.add_edge(START, "case-design")
    builder.add_edge("case-design", "case-review")
    builder.add_edge("case-review", "done")
    builder.add_edge("done", END)
    return context.compile_subgraph(builder)


__all__ = ["build_case_graph"]
