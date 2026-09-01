from __future__ import annotations

from langgraph.graph.state import CompiledStateGraph

from assurance_generation.graphs.api import compile_family_graph
from graph_engine.boot.boot import CapabilityBuildContext


def build_performance_graph(context: CapabilityBuildContext) -> CompiledStateGraph:
    return compile_family_graph(context, "performance", has_codegen_fix=False)


__all__ = ["build_performance_graph"]
