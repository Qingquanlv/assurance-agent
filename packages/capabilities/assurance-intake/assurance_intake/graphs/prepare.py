from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from assurance_intake.graphs.nodes import terminal_failed, terminal_prepared
from assurance_intake.graphs.routes import route_preparation_attempt
from assurance_intake.graphs.state import IntakeState
from graph_engine.boot.boot import CapabilityBuildContext


def _node(fn: object) -> Callable[..., Any]:
    return cast(Callable[..., Any], fn)


def build_prepare_graph(
    context: CapabilityBuildContext,
    *,
    intake: CompiledStateGraph,
    explore: CompiledStateGraph,
    resolve_plan: CompiledStateGraph,
) -> CompiledStateGraph:
    builder: StateGraph[IntakeState] = StateGraph(IntakeState)
    builder.add_node("intake", intake)
    builder.add_node("explore", explore)
    builder.add_node("resolve-plan", resolve_plan)
    builder.add_node("prepared", _node(terminal_prepared))
    builder.add_node("failed", _node(terminal_failed))
    builder.add_edge(START, "intake")
    builder.add_conditional_edges(
        "intake",
        cast(Callable[..., Any], route_preparation_attempt),
        {"committed": "explore", "failed": "failed"},
    )
    builder.add_conditional_edges(
        "explore",
        cast(Callable[..., Any], route_preparation_attempt),
        {"committed": "resolve-plan", "failed": "failed"},
    )
    builder.add_conditional_edges(
        "resolve-plan",
        cast(Callable[..., Any], route_preparation_attempt),
        {"committed": "prepared", "failed": "failed"},
    )
    builder.add_edge("prepared", END)
    builder.add_edge("failed", END)
    return context.compile_subgraph(builder)


def build_load_plan_graph(
    context: CapabilityBuildContext,
    *,
    load_plan: CompiledStateGraph,
) -> CompiledStateGraph:
    builder: StateGraph[IntakeState] = StateGraph(IntakeState)
    builder.add_node("load-plan", load_plan)
    builder.add_node("prepared", _node(terminal_prepared))
    builder.add_node("failed", _node(terminal_failed))
    builder.add_edge(START, "load-plan")
    builder.add_conditional_edges(
        "load-plan",
        cast(Callable[..., Any], route_preparation_attempt),
        {"committed": "prepared", "failed": "failed"},
    )
    builder.add_edge("prepared", END)
    builder.add_edge("failed", END)
    return context.compile_subgraph(builder)


__all__ = ["build_load_plan_graph", "build_prepare_graph"]
