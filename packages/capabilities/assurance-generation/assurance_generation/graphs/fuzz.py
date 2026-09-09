from __future__ import annotations

from langgraph.graph.state import CompiledStateGraph

from assurance_generation.graphs.api import compile_family_graph
from graph_engine.boot.boot import CapabilityBuildContext


def build_fuzz_graph(context: CapabilityBuildContext) -> CompiledStateGraph:
    return compile_family_graph(context, "fuzz")


__all__ = ["build_fuzz_graph"]
