from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from assurance_generation.graphs.api import compile_family_pair
from assurance_generation.graphs.nodes import complete_generation_node, generation_done, join_selected
from assurance_generation.graphs.routes import route_families
from assurance_generation.graphs.state import GenerationState
from graph_engine.boot.boot import CapabilityBuildContext


@dataclass(frozen=True, slots=True)
class GenerationGraphs:
    generation: CompiledStateGraph
    api: CompiledStateGraph
    e2e: CompiledStateGraph
    fuzz: CompiledStateGraph
    performance: CompiledStateGraph


def _build_root_graph(
    context: CapabilityBuildContext,
    *,
    api: CompiledStateGraph,
    e2e: CompiledStateGraph,
    fuzz: CompiledStateGraph,
    performance: CompiledStateGraph,
) -> CompiledStateGraph:
    builder: StateGraph[GenerationState] = StateGraph(GenerationState)
    builder.add_node("fanout", cast(Callable[..., Any], lambda _state: {}))
    builder.add_node("api", api)
    builder.add_node("e2e", e2e)
    builder.add_node("fuzz", fuzz)
    builder.add_node("performance", performance)
    builder.add_node("join-selected", cast(Callable[..., Any], join_selected))
    builder.add_node("complete", cast(Callable[..., Any], complete_generation_node))
    builder.add_node("done", cast(Callable[..., Any], generation_done))
    builder.add_edge(START, "fanout")
    builder.add_conditional_edges("fanout", cast(Callable[..., Any], route_families))
    builder.add_edge("api", "join-selected")
    builder.add_edge("e2e", "join-selected")
    builder.add_edge("fuzz", "join-selected")
    builder.add_edge("performance", "join-selected")
    builder.add_edge("join-selected", "complete")
    builder.add_edge("complete", "done")
    builder.add_edge("done", END)
    return context.compile_subgraph(builder)


def build_generation_graphs(context: CapabilityBuildContext) -> GenerationGraphs:
    api, api_lane = compile_family_pair(context, "api", has_codegen_fix=True)
    e2e, e2e_lane = compile_family_pair(context, "e2e", has_codegen_fix=True)
    fuzz, fuzz_lane = compile_family_pair(context, "fuzz", has_codegen_fix=False)
    performance, performance_lane = compile_family_pair(context, "performance", has_codegen_fix=False)
    return GenerationGraphs(
        generation=_build_root_graph(
            context,
            api=api_lane,
            e2e=e2e_lane,
            fuzz=fuzz_lane,
            performance=performance_lane,
        ),
        api=api,
        e2e=e2e,
        fuzz=fuzz,
        performance=performance,
    )


__all__ = ["GenerationGraphs", "build_generation_graphs"]
