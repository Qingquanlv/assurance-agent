from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, cast

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from graph_engine.boot.boot import CapabilityBuildContext

from assurance_generation.graphs.nodes import (
    activation_init_runtime,
    publish_init_runtime,
    select_init_runtime,
)
from assurance_generation.graphs.routes import route_attempt_result
from assurance_generation.graphs.state import GenerationState

_INIT_TEST_RUNTIME_ID = "assurance.generation.init-test-runtime"


def terminal_done(state: Mapping[str, object]) -> dict[str, object]:
    status = "failed" if state.get("attempt_failure") else "completed"
    return {"status": status}


def build_init_runtime_graph(context: CapabilityBuildContext) -> CompiledStateGraph:
    builder: StateGraph[GenerationState] = StateGraph(GenerationState)
    builder.add_node(
        "generation.init-test-runtime",
        cast(
            Callable[..., Any],
            context.attempt(
                _INIT_TEST_RUNTIME_ID,
                semantic_node_id="generation.init-test-runtime",
                activation=activation_init_runtime,
                select=select_init_runtime,
                publish=publish_init_runtime,
            ),
        ),
    )
    builder.add_node("done", cast(Callable[..., Any], terminal_done))
    builder.add_node("failed", cast(Callable[..., Any], terminal_done))
    builder.add_edge(START, "generation.init-test-runtime")
    builder.add_conditional_edges(
        "generation.init-test-runtime",
        cast(Callable[..., Any], route_attempt_result),
        {"committed": "done", "failed": "failed"},
    )
    builder.add_edge("done", END)
    builder.add_edge("failed", END)
    return context.compile_subgraph(builder)


__all__ = ["build_init_runtime_graph"]
