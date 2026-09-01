from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, cast

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from assurance_generation.graphs.api import build_api_graph
from assurance_generation.graphs.e2e import build_e2e_graph
from assurance_generation.graphs.fuzz import build_fuzz_graph
from assurance_generation.graphs.nodes import complete_generation_node, join_selected, terminal_done
from assurance_generation.graphs.performance import build_performance_graph
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


def _family_result_only(graph: CompiledStateGraph) -> Callable[..., Any]:
    async def _run(state: Mapping[str, object]) -> dict[str, object]:
        result = await graph.ainvoke(dict(state))
        results = result.get("family_results") if isinstance(result, Mapping) else None
        return {"family_results": list(results or [])}

    return _run


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
    builder.add_node("api", _family_result_only(api))
    builder.add_node("e2e", _family_result_only(e2e))
    builder.add_node("fuzz", _family_result_only(fuzz))
    builder.add_node("performance", _family_result_only(performance))
    builder.add_node("join-selected", cast(Callable[..., Any], join_selected))
    builder.add_node("complete", cast(Callable[..., Any], complete_generation_node))
    builder.add_node("done", cast(Callable[..., Any], terminal_done))
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
    api = build_api_graph(context)
    e2e = build_e2e_graph(context)
    fuzz = build_fuzz_graph(context)
    performance = build_performance_graph(context)
    return GenerationGraphs(
        generation=_build_root_graph(context, api=api, e2e=e2e, fuzz=fuzz, performance=performance),
        api=api,
        e2e=e2e,
        fuzz=fuzz,
        performance=performance,
    )


__all__ = ["GenerationGraphs", "build_generation_graphs"]
