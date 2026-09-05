from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from assurance_product.graphs.entrypoints import (
    adapt_improvement,
    adapt_intake,
    publish_public_output,
    validate_public_input,
)
from assurance_product.graphs.execute import adapt_execute_tail_input
from assurance_product.graphs.routes import route_case, route_execute_tail, route_prepare
from assurance_product.graphs.state import ProductState
from graph_engine.boot.boot import GraphBuildContext


def adapt_execute_tail(state: ProductState) -> dict[str, object]:
    return adapt_execute_tail_input(state, standalone=False)


def _terminal_achieved(state: ProductState) -> dict[str, object]:
    published = publish_public_output(cast(ProductState, {**dict(state), "status": "completed"}))
    return {**published, "terminal": "achieved", "status": "completed"}


def _terminal_not_achieved(state: ProductState) -> dict[str, object]:
    published = publish_public_output(cast(ProductState, {**dict(state), "status": "failed"}))
    return {**published, "terminal": "not-achieved", "status": "failed"}


def build_full_graph(bundles: object, execute: CompiledStateGraph) -> StateGraph[ProductState]:
    typed = cast(Any, bundles)
    builder: StateGraph[ProductState] = StateGraph(ProductState)
    builder.add_node("validate", validate_public_input("full"))
    builder.add_node("adapt-prepare", cast(Any, adapt_intake))
    builder.add_node("prepare", typed.intake.prepare)
    builder.add_node("case", typed.intake.case)
    builder.add_node("adapt-execute-tail", cast(Any, adapt_execute_tail))
    builder.add_node("execute-tail", execute)
    builder.add_node("adapt-retro", cast(Any, adapt_improvement))
    builder.add_node("retro", typed.improvement.retro)
    builder.add_node("adapt-improvement", cast(Any, adapt_improvement))
    builder.add_node("improvement", typed.improvement.apply)
    builder.add_node("achieved", cast(Any, _terminal_achieved))
    builder.add_node("not-achieved", cast(Any, _terminal_not_achieved))
    builder.add_edge(START, "validate")
    builder.add_edge("validate", "adapt-prepare")
    builder.add_edge("adapt-prepare", "prepare")
    builder.add_conditional_edges(
        "prepare",
        cast(Callable[..., Any], route_prepare),
        {"prepared": "case", "failed": "not-achieved"},
    )
    builder.add_conditional_edges(
        "case",
        cast(Callable[..., Any], route_case),
        {"execute-tail": "adapt-execute-tail", "not-achieved": "not-achieved"},
    )
    builder.add_edge("adapt-execute-tail", "execute-tail")
    builder.add_conditional_edges(
        "execute-tail",
        cast(Callable[..., Any], route_execute_tail),
        {"retro": "adapt-retro", "not-achieved": "not-achieved"},
    )
    builder.add_edge("adapt-retro", "retro")
    builder.add_edge("retro", "adapt-improvement")
    builder.add_edge("adapt-improvement", "improvement")
    builder.add_edge("improvement", "achieved")
    builder.add_edge("achieved", END)
    builder.add_edge("not-achieved", END)
    return builder


def build_full_root(
    context: GraphBuildContext,
    bundles: object,
    execute: CompiledStateGraph,
) -> CompiledStateGraph:
    return context.compile_root(build_full_graph(bundles, execute))


__all__ = ["adapt_execute_tail", "build_full_graph", "build_full_root"]
