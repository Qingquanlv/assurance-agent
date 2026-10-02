from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, cast

from langgraph.graph import END, START
from langgraph.graph.state import CompiledStateGraph

from assurance_generation.contracts.attempts import TASK_ATTEMPT_CONTRACTS
from assurance_generation.graphs.nodes import (
    activation_init_runtime,
    publish_init_runtime,
    select_init_runtime,
)
from assurance_generation.graphs.state import GenerationState
from graph_engine.boot.boot import CapabilityBuildContext
from graph_engine.stategraph import AttemptGraph


def _node(fn: object) -> Callable[..., Any]:
    return cast(Callable[..., Any], fn)


def route_attempt_result(state: Mapping[str, object]) -> str:
    return "failed" if state.get("attempt_failure") else "committed"


def terminal_done(state: Mapping[str, object]) -> dict[str, object]:
    status = "failed" if state.get("attempt_failure") else "completed"
    return {"status": status}


def build_init_runtime_graph(context: CapabilityBuildContext) -> CompiledStateGraph:
    builder: AttemptGraph[GenerationState] = AttemptGraph(
        GenerationState,
        context,
        namespace="generation",
    )
    builder.add_attempt(
        "generation.init-test-runtime",
        TASK_ATTEMPT_CONTRACTS["init-test-runtime"],
        select=select_init_runtime,
        publish=publish_init_runtime,
        activation=activation_init_runtime,
        semantic_node_id="generation.init-test-runtime",
    )
    builder.add_node("done", _node(terminal_done))
    builder.add_node("failed", _node(terminal_done))
    builder.add_edge(START, "generation.init-test-runtime")
    builder.add_conditional_edges(
        "generation.init-test-runtime",
        _node(route_attempt_result),
        {"committed": "done", "failed": "failed"},
    )
    builder.add_edge("done", END)
    builder.add_edge("failed", END)
    return builder.compile_subgraph()


__all__ = ["build_init_runtime_graph", "route_attempt_result"]
