from __future__ import annotations

from collections.abc import Callable, Hashable, Mapping
from typing import Any, cast

from langgraph.graph import END, START
from langgraph.graph.state import CompiledStateGraph

from graph_engine.boot.boot import CapabilityBuildContext
from graph_engine.stategraph import AttemptGraph
from graph_engine.stategraph.routing import select_exclusive_route

from assurance_quality.contracts.assessment import InspectionDisposition
from assurance_quality.graphs.issues import route_attempt
from assurance_quality.graphs.nodes import (
    activation_assess,
    activation_materialize_assessment,
    publish_inspect,
    publish_materialize_assessment,
    select_inspect,
    select_materialize_assessment,
    terminal_done,
)
from assurance_quality.graphs.state import QualityState

_INSPECT_ID = "assurance.quality.agent.inspect.v1"
_MATERIALIZE_ID = "assurance.quality.materialize-assessment-inputs"
_COVERAGE_OTHERWISE = "failed"
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
_MATERIALIZE_TARGETS = ("quality.inspect", "failed")


def coverage_named_matches(state: Mapping[str, object]) -> dict[str, str | None]:
    raw = state.get("inspection_outcome")
    current = raw.get("disposition") if isinstance(raw, Mapping) else getattr(raw, "disposition", None)
    return {name: name if current == name else None for name in _DISPOSITIONS}


def route_coverage(state: Mapping[str, object]) -> str:
    if state.get("attempt_failure"):
        return _COVERAGE_OTHERWISE
    return select_exclusive_route(coverage_named_matches(state), otherwise=_COVERAGE_OTHERWISE)


def _node(fn: object) -> Callable[..., Any]:
    return cast(Callable[..., Any], fn)


def build_assess_graph(context: CapabilityBuildContext) -> CompiledStateGraph:
    builder: AttemptGraph[QualityState] = AttemptGraph(QualityState, context, namespace="quality")
    builder.add_attempt(
        "quality.materialize-assessment-inputs",
        _MATERIALIZE_ID,
        select=select_materialize_assessment,
        publish=publish_materialize_assessment,
        activation=activation_materialize_assessment,
        semantic_node_id="quality.materialize-assessment-inputs",
    )
    builder.add_attempt(
        "quality.inspect",
        _INSPECT_ID,
        select=select_inspect,
        publish=publish_inspect,
        activation=activation_assess,
        semantic_node_id="quality.inspect",
    )
    for name in _COVERAGE_PATHS:
        if name == "analysis_required":
            continue
        builder.add_node(str(name), _node(terminal_done))
        builder.add_edge(str(name), END)
    builder.add_edge(START, "quality.materialize-assessment-inputs")
    builder.add_route(
        "quality.materialize-assessment-inputs",
        route_attempt("quality.inspect"),
        targets=_MATERIALIZE_TARGETS,
    )
    builder.add_conditional_edges(
        "quality.inspect",
        _node(route_coverage),
        _COVERAGE_PATHS,
    )
    return builder.compile_subgraph()


__all__ = ["build_assess_graph", "coverage_named_matches", "route_coverage"]
