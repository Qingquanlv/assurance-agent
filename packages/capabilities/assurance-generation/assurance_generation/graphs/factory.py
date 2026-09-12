from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from assurance_generation.graphs.api import compile_family_pair
from assurance_generation.graphs.init_runtime import build_init_runtime_graph
from assurance_generation.graphs.nodes import (
    activation_generation_cycle,
    activation_generation_inputs,
    complete_generation_node,
    generation_done,
    join_selected,
    publish_generation_inputs,
    select_generation_inputs,
    select_generation_cycle,
    publish_generation_cycle,
    route_generation_completion,
)
from assurance_generation.graphs.routes import route_attempt_result, route_families
from assurance_generation.graphs.state import GenerationState
from graph_engine.boot.boot import CapabilityBuildContext


@dataclass(frozen=True, slots=True)
class GenerationGraphs:
    generation: CompiledStateGraph
    api: CompiledStateGraph
    e2e: CompiledStateGraph
    fuzz: CompiledStateGraph
    performance: CompiledStateGraph
    init_runtime: CompiledStateGraph
    resolve_inputs: CompiledStateGraph


def _resolve_inputs_attempt(context: CapabilityBuildContext) -> Callable[..., Any]:
    return cast(
        Callable[..., Any],
        context.attempt(
            "assurance.generation.resolve-inputs",
            semantic_node_id="generation.resolve-inputs",
            activation=activation_generation_inputs,
            select=select_generation_inputs,
            publish=publish_generation_inputs,
        ),
    )


def _build_resolve_inputs_graph(context: CapabilityBuildContext) -> CompiledStateGraph:
    builder: StateGraph[GenerationState] = StateGraph(GenerationState)
    builder.add_node("generation.resolve-inputs", _resolve_inputs_attempt(context))
    builder.add_node(
        "done",
        cast(
            Callable[..., Any],
            lambda state: {"status": "failed" if state.get("attempt_failure") else "completed"},
        ),
    )
    builder.add_edge(START, "generation.resolve-inputs")
    builder.add_edge("generation.resolve-inputs", "done")
    builder.add_edge("done", END)
    return context.compile_subgraph(builder)


def _build_root_graph(
    context: CapabilityBuildContext,
    *,
    api: CompiledStateGraph,
    e2e: CompiledStateGraph,
    fuzz: CompiledStateGraph,
    performance: CompiledStateGraph,
) -> CompiledStateGraph:
    builder: StateGraph[GenerationState] = StateGraph(GenerationState)
    builder.add_node(
        "generation.resolve-inputs",
        _resolve_inputs_attempt(context),
    )
    builder.add_node("fanout", cast(Callable[..., Any], lambda _state: {}))
    builder.add_node("api", api)
    builder.add_node("e2e", e2e)
    builder.add_node("fuzz", fuzz)
    builder.add_node("performance", performance)
    builder.add_node("join-selected", cast(Callable[..., Any], join_selected))
    builder.add_node("complete", cast(Callable[..., Any], complete_generation_node))
    builder.add_node(
        "generation.publish-cycle",
        cast(
            Callable[..., Any],
            context.attempt(
                "assurance.generation.publish-cycle",
                semantic_node_id="generation.publish-cycle",
                activation=activation_generation_cycle,
                select=select_generation_cycle,
                publish=publish_generation_cycle,
            ),
        ),
    )
    builder.add_node("done", cast(Callable[..., Any], generation_done))
    builder.add_edge(START, "generation.resolve-inputs")
    builder.add_conditional_edges(
        "generation.resolve-inputs",
        cast(Callable[..., Any], route_attempt_result),
        {"committed": "fanout", "failed": "done"},
    )
    builder.add_conditional_edges("fanout", cast(Callable[..., Any], route_families))
    builder.add_edge("api", "join-selected")
    builder.add_edge("e2e", "join-selected")
    builder.add_edge("fuzz", "join-selected")
    builder.add_edge("performance", "join-selected")
    builder.add_edge("join-selected", "complete")
    builder.add_conditional_edges(
        "complete",
        cast(Callable[..., Any], route_generation_completion),
        {"publish": "generation.publish-cycle", "failed": "done"},
    )
    builder.add_edge("generation.publish-cycle", "done")
    builder.add_edge("done", END)
    return context.compile_subgraph(builder)


def build_generation_graphs(context: CapabilityBuildContext) -> GenerationGraphs:
    api, api_lane = compile_family_pair(context, "api")
    e2e, e2e_lane = compile_family_pair(context, "e2e")
    fuzz, fuzz_lane = compile_family_pair(context, "fuzz")
    performance, performance_lane = compile_family_pair(context, "performance")
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
        init_runtime=build_init_runtime_graph(context),
        resolve_inputs=_build_resolve_inputs_graph(context),
    )


__all__ = ["GenerationGraphs", "build_generation_graphs"]
