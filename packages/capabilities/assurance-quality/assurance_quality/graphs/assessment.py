from __future__ import annotations

from collections.abc import Callable, Hashable
from typing import Any, cast

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from graph_engine.boot.boot import CapabilityBuildContext

from assurance_quality.contracts.assessment import InspectionDisposition
from assurance_quality.graphs.nodes import (
    activation_assess,
    activation_materialize_assessment,
    publish_fact_baseline,
    publish_inspect,
    publish_materialize_assessment,
    select_materialize_assessment,
    select_fact_baseline,
    select_inspect,
    terminal_done,
)
from assurance_quality.graphs.routes import route_attempt, route_coverage
from assurance_quality.graphs.state import QualityState

_FACT_BASELINE_ID = "assurance.quality.agent.fact-baseline.v1"
_INSPECT_ID = "assurance.quality.agent.inspect.v1"
_MATERIALIZE_ID = "assurance.quality.materialize-assessment-inputs"
_DISPOSITIONS: tuple[InspectionDisposition, ...] = (
    "satisfied",
    "coverage_insufficient",
    "repairable_execution_failure",
    "analysis_required",
    "needs_human",
    "blocked",
)
_COVERAGE_PATHS: dict[Hashable, str] = {
    name: END if name == "analysis_required" else name for name in (*_DISPOSITIONS, "failed")
}
_ATTEMPT_PATHS: dict[Hashable, str] = {"ready": "quality.fact-baseline", "failed": "failed"}


def build_assess_graph(context: CapabilityBuildContext) -> CompiledStateGraph:
    builder: StateGraph[QualityState] = StateGraph(QualityState)
    builder.add_node(
        "quality.materialize-assessment-inputs",
        cast(
            Callable[..., Any],
            context.attempt(
                _MATERIALIZE_ID,
                semantic_node_id="quality.materialize-assessment-inputs",
                activation=activation_materialize_assessment,
                select=select_materialize_assessment,
                publish=publish_materialize_assessment,
            ),
        ),
    )
    builder.add_node(
        "quality.fact-baseline",
        cast(
            Callable[..., Any],
            context.attempt(
                _FACT_BASELINE_ID,
                semantic_node_id="quality.fact-baseline",
                activation=activation_assess,
                select=select_fact_baseline,
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
                select=select_inspect,
                publish=publish_inspect,
            ),
        ),
    )
    for name in _COVERAGE_PATHS:
        if name == "analysis_required":
            continue
        builder.add_node(str(name), cast(Callable[..., Any], terminal_done))
        builder.add_edge(str(name), END)
    builder.add_edge(START, "quality.materialize-assessment-inputs")
    builder.add_conditional_edges(
        "quality.materialize-assessment-inputs",
        cast(Callable[..., Any], route_attempt),
        _ATTEMPT_PATHS,
    )
    builder.add_conditional_edges(
        "quality.fact-baseline",
        cast(Callable[..., Any], route_attempt),
        {"ready": "quality.inspect", "failed": "failed"},
    )
    builder.add_conditional_edges(
        "quality.inspect",
        cast(Callable[..., Any], route_coverage),
        _COVERAGE_PATHS,
    )
    return context.compile_subgraph(builder)


__all__ = ["build_assess_graph"]
