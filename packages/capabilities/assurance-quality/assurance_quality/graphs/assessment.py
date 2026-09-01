from __future__ import annotations

from collections.abc import Callable, Hashable
from typing import Any, cast

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from graph_engine.boot.boot import CapabilityBuildContext

from assurance_quality.contracts.workflow import COVERAGE_STATES
from assurance_quality.graphs.nodes import (
    activation_assess,
    publish_fact_baseline,
    publish_inspect,
    select_quality,
    terminal_done,
)
from assurance_quality.graphs.routes import route_coverage
from assurance_quality.graphs.state import QualityState

_FACT_BASELINE_ID = "assurance.quality.agent.fact-baseline.v1"
_INSPECT_ID = "assurance.quality.agent.inspect.v1"
_COVERAGE_PATHS: dict[Hashable, str] = {name: name for name in (*COVERAGE_STATES, "failed")}


def build_assess_graph(context: CapabilityBuildContext) -> CompiledStateGraph:
    builder: StateGraph[QualityState] = StateGraph(QualityState)
    builder.add_node(
        "quality.fact-baseline",
        cast(
            Callable[..., Any],
            context.attempt(
                _FACT_BASELINE_ID,
                semantic_node_id="quality.fact-baseline",
                activation=activation_assess,
                select=select_quality,
                publish=publish_fact_baseline,
            ),
        ),
    )
    builder.add_node(
        "quality.inspect",
        cast(
            Callable[..., Any],
            context.attempt(
                _INSPECT_ID,
                semantic_node_id="quality.inspect",
                activation=activation_assess,
                select=select_quality,
                publish=publish_inspect,
            ),
        ),
    )
    for name in _COVERAGE_PATHS:
        builder.add_node(str(name), cast(Callable[..., Any], terminal_done))
        builder.add_edge(str(name), END)
    builder.add_edge(START, "quality.fact-baseline")
    builder.add_edge("quality.fact-baseline", "quality.inspect")
    builder.add_conditional_edges(
        "quality.inspect",
        cast(Callable[..., Any], route_coverage),
        _COVERAGE_PATHS,
    )
    return context.compile_subgraph(builder)


__all__ = ["build_assess_graph"]
