from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from graph_engine.boot.boot import CapabilityBuildContext

from assurance_quality.graphs.nodes import (
    activation_one_shot,
    publish_report,
    select_report,
    terminal_done,
)
from assurance_quality.graphs.state import QualityState

_REPORT_ID = "assurance.quality.agent.report.v1"


def build_report_graph(context: CapabilityBuildContext) -> CompiledStateGraph:
    builder: StateGraph[QualityState] = StateGraph(QualityState)
    builder.add_node(
        "quality.report",
        cast(
            Callable[..., Any],
            context.attempt(
                _REPORT_ID,
                semantic_node_id="quality.report",
                activation=activation_one_shot,
                select=select_report,
                publish=publish_report,
            ),
        ),
    )
    builder.add_node("done", cast(Callable[..., Any], terminal_done))
    builder.add_edge(START, "quality.report")
    builder.add_edge("quality.report", "done")
    builder.add_edge("done", END)
    return context.compile_subgraph(builder)


__all__ = ["build_report_graph"]
