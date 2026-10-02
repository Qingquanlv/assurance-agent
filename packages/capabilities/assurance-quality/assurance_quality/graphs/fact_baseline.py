from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

from langgraph.graph import END, START
from langgraph.graph.state import CompiledStateGraph

from graph_engine.boot.boot import CapabilityBuildContext
from graph_engine.stategraph import AttemptGraph

from assurance_quality.graphs.issues import route_attempt
from assurance_quality.graphs.nodes import (
    activation_fact_baseline,
    publish_fact_baseline,
    select_fact_baseline,
    terminal_done,
)
from assurance_quality.graphs.state import QualityState
from assurance_quality.ops.fact_baseline import op as fact_baseline

_ATTEMPT_TARGETS = ("done", "failed")


def _node(fn: object) -> Callable[..., Any]:
    return cast(Callable[..., Any], fn)


def build_fact_baseline_graph(context: CapabilityBuildContext) -> CompiledStateGraph:
    builder: AttemptGraph[QualityState] = AttemptGraph(QualityState, context, namespace="quality")
    builder.add_attempt(
        "quality.fact-baseline",
        fact_baseline,
        select=select_fact_baseline,
        publish=publish_fact_baseline,
        activation=activation_fact_baseline,
        semantic_node_id="quality.fact-baseline",
    )
    builder.add_node("done", _node(terminal_done))
    builder.add_node("failed", _node(terminal_done))
    builder.add_edge(START, "quality.fact-baseline")
    builder.add_route(
        "quality.fact-baseline",
        route_attempt("done"),
        targets=_ATTEMPT_TARGETS,
    )
    builder.add_edge("done", END)
    builder.add_edge("failed", END)
    return builder.compile_subgraph()


__all__ = ["build_fact_baseline_graph"]
