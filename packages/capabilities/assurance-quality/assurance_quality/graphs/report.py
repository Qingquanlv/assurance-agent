from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, cast

from langgraph.graph import END, START
from langgraph.graph.state import CompiledStateGraph

from graph_engine.boot.boot import CapabilityBuildContext
from graph_engine.stategraph import AttemptGraph
from graph_engine.stategraph.routing import select_exclusive_route

from assurance_quality.graphs.nodes import (
    activation_one_shot,
    clear_report_state,
    publish_report,
    select_report,
    terminal_done,
)
from assurance_quality.graphs.state import QualityState

_REPORT_ID = "assurance.quality.agent.report.v1"
_REPORT_OTHERWISE = "failed"
_REPORT_TABLE: dict[str, Callable[[Mapping[str, object]], bool]] = {
    "done": lambda state: bool(state.get("report_outcome") or state.get("report_purpose") == "diagnostic"),
}


def route_report_attempt(state: Mapping[str, object]) -> str:
    if state.get("attempt_failure"):
        return _REPORT_OTHERWISE
    return select_exclusive_route(
        {name: name if predicate(state) else None for name, predicate in _REPORT_TABLE.items()},
        otherwise=_REPORT_OTHERWISE,
    )


def _node(fn: object) -> Callable[..., Any]:
    return cast(Callable[..., Any], fn)


def build_report_graph(context: CapabilityBuildContext) -> CompiledStateGraph:
    builder: AttemptGraph[QualityState] = AttemptGraph(QualityState, context, namespace="quality")
    builder.add_node("clear-report", _node(clear_report_state))
    builder.add_attempt(
        "quality.report",
        _REPORT_ID,
        select=select_report,
        publish=publish_report,
        activation=activation_one_shot,
        semantic_node_id="quality.report",
    )
    builder.add_node("done", _node(terminal_done))
    builder.add_node("failed", _node(terminal_done))
    builder.add_edge(START, "clear-report")
    builder.add_edge("clear-report", "quality.report")
    builder.add_route(
        "quality.report",
        route_report_attempt,
        targets=(*_REPORT_TABLE, _REPORT_OTHERWISE),
    )
    builder.add_edge("done", END)
    builder.add_edge("failed", END)
    return builder.compile_subgraph()


__all__ = ["build_report_graph", "route_report_attempt"]
