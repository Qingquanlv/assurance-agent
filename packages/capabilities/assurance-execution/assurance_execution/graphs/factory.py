from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import Any, Literal, cast

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from assurance_execution.graphs.nodes import (
    activation_execute,
    activation_rerun,
    publish_execution,
    route_execution,
    select_execute,
    select_rerun,
    terminal_committed,
    terminal_failed,
)
from assurance_execution.graphs.state import ExecutionState
from graph_engine.boot.boot import CapabilityBuildContext

_EXECUTE_CONTRACT = "assurance.execution.task.execute.v1"
_RUN_CONTRACT = "assurance.execution.task.run.v1"
_TERMINALS = {"committed": "committed", "failed": "failed"}
ExecutionSemanticNodeId = Literal["execution.execute", "execution.run"]


@dataclass(frozen=True, slots=True)
class ExecutionGraphs:
    execute: CompiledStateGraph
    rerun: CompiledStateGraph


def build_execution_graphs(context: CapabilityBuildContext) -> ExecutionGraphs:
    return ExecutionGraphs(
        execute=_compile_graph(
            context,
            contract_id=_EXECUTE_CONTRACT,
            semantic_node_id="execution.execute",
            activation=activation_execute,
            select=select_execute,
        ),
        rerun=_compile_graph(
            context,
            contract_id=_RUN_CONTRACT,
            semantic_node_id="execution.run",
            activation=activation_rerun,
            select=select_rerun,
        ),
    )


def _compile_graph(
    context: CapabilityBuildContext,
    *,
    contract_id: str,
    semantic_node_id: ExecutionSemanticNodeId,
    activation: object,
    select: object,
) -> CompiledStateGraph:
    builder: StateGraph[ExecutionState] = StateGraph(ExecutionState)
    builder.add_node(
        semantic_node_id,
        cast(
            Callable[..., Any],
            context.attempt(
                contract_id,
                semantic_node_id=semantic_node_id,
                activation=activation,
                select=select,
                publish=partial(publish_execution, semantic_node_id=semantic_node_id),
            ),
        ),
    )
    builder.add_node("committed", terminal_committed)
    builder.add_node("failed", terminal_failed)
    builder.add_edge(START, semantic_node_id)
    builder.add_conditional_edges(semantic_node_id, route_execution, list(_TERMINALS))
    builder.add_edge("committed", END)
    builder.add_edge("failed", END)
    return context.compile_subgraph(builder)


__all__ = ["ExecutionGraphs", "build_execution_graphs"]
