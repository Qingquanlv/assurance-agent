from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from graph_engine.boot.boot import CapabilityBuildContext
from graph_engine.stategraph import add_attempt_node

from assurance_quality.graphs.nodes import (
    activation_one_shot,
    clear_report_state,
    publish_report,
    route_report_attempt,
    select_report,
    terminal_done,
)
from assurance_quality.graphs.state import QualityState

_REPORT_ID = "assurance.quality.agent.report.v1"


def build_report_graph(context: CapabilityBuildContext) -> CompiledStateGraph:
    builder: StateGraph[QualityState] = StateGraph(QualityState)
    builder.add_node("clear-report", cast(Callable[..., Any], clear_report_state))
    add_attempt_node(
        builder,
        context,
        "quality.report",
        contract_id=_REPORT_ID,
        activation=activation_one_shot,
        select=select_report,
        publish=publish_report,
    )
    builder.add_node("done", cast(Callable[..., Any], terminal_done))
    builder.add_node("failed", cast(Callable[..., Any], terminal_done))
    builder.add_edge(START, "clear-report")
    builder.add_edge("clear-report", "quality.report")
    builder.add_conditional_edges(
        "quality.report",
        cast(Callable[..., Any], route_report_attempt),
        {"done": "done", "failed": "failed"},
    )
    builder.add_edge("done", END)
    builder.add_edge("failed", END)
    return context.compile_subgraph(builder)


__all__ = ["build_report_graph"]
