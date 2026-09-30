from __future__ import annotations

from collections.abc import Callable, Hashable
from typing import Any, cast

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from assurance_intake.feature import IntakeGraphs
from assurance_intake.graphs.case import build_case_graph
from assurance_intake.graphs.nodes import (
    activation_case_design,
    activation_case_design_repair,
    publish_case_design,
    select_case_design,
    select_case_design_repair,
    terminal_done,
    terminal_failed,
)
from assurance_intake.graphs.prepare import build_prepare_graph
from assurance_intake.graphs.routes import route_case_design, route_case_design_repair
from assurance_intake.graphs.state import IntakeState
from graph_engine.boot.boot import CapabilityBuildContext
from graph_engine.stategraph import add_attempt_node

_CASE_DESIGN_ID = "assurance.intake.agent.case-design.v1"
_CASE_DESIGN_PATHS: dict[Hashable, str] = {
    "done": "done",
    "case-design-repair": "intake.case-design-repair",
    "failed": "failed",
}


def build_intake_graphs(context: CapabilityBuildContext) -> IntakeGraphs:
    return IntakeGraphs(
        prepare=build_prepare_graph(context),
        case=build_case_graph(context, case_design=_compile_case_design(context)),
    )


def _compile_case_design(context: CapabilityBuildContext) -> CompiledStateGraph:
    builder: StateGraph[IntakeState] = StateGraph(IntakeState)
    add_attempt_node(
        builder,
        context,
        "intake.case-design",
        semantic_node_id="intake.case-design",
        contract_id=_CASE_DESIGN_ID,
        activation=activation_case_design,
        select=select_case_design,
        publish=publish_case_design,
    )
    add_attempt_node(
        builder,
        context,
        "intake.case-design-repair",
        semantic_node_id="intake.case-design-repair",
        contract_id=_CASE_DESIGN_ID,
        activation=activation_case_design_repair,
        select=select_case_design_repair,
        publish=publish_case_design,
    )
    builder.add_node("done", cast(Callable[..., Any], terminal_done))
    builder.add_node("failed", cast(Callable[..., Any], terminal_failed))
    builder.add_edge(START, "intake.case-design")
    builder.add_conditional_edges(
        "intake.case-design",
        cast(Callable[..., Any], route_case_design),
        _CASE_DESIGN_PATHS,
    )
    builder.add_conditional_edges(
        "intake.case-design-repair",
        cast(Callable[..., Any], route_case_design_repair),
        {"done": "done", "failed": "failed"},
    )
    builder.add_edge("done", END)
    builder.add_edge("failed", END)
    return context.compile_subgraph(builder)


__all__ = ["IntakeGraphs", "build_intake_graphs"]
