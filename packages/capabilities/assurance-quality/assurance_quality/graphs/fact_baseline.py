from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from graph_engine.boot.boot import CapabilityBuildContext
from graph_engine.stategraph import add_attempt_node

from assurance_quality.graphs.nodes import (
    activation_fact_baseline,
    publish_fact_baseline,
    select_fact_baseline,
    terminal_done,
)
from assurance_quality.graphs.routes import route_attempt
from assurance_quality.graphs.state import QualityState

_FACT_BASELINE_ID = "assurance.quality.agent.fact-baseline.v1"


def build_fact_baseline_graph(context: CapabilityBuildContext) -> CompiledStateGraph:
    builder: StateGraph[QualityState] = StateGraph(QualityState)
    add_attempt_node(
        builder,
        context,
        "quality.fact-baseline",
        contract_id=_FACT_BASELINE_ID,
        activation=activation_fact_baseline,
        select=select_fact_baseline,
        publish=publish_fact_baseline,
    )
    builder.add_node("done", cast(Callable[..., Any], terminal_done))
    builder.add_node("failed", cast(Callable[..., Any], terminal_done))
    builder.add_edge(START, "quality.fact-baseline")
    builder.add_conditional_edges(
        "quality.fact-baseline",
        cast(Callable[..., Any], route_attempt),
        {"ready": "done", "failed": "failed"},
    )
    builder.add_edge("done", END)
    builder.add_edge("failed", END)
    return context.compile_subgraph(builder)


__all__ = ["build_fact_baseline_graph"]
