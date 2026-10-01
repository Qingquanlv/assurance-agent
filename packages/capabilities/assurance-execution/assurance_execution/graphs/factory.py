from __future__ import annotations

from collections.abc import Callable, Mapping
from functools import partial
from typing import Any, Literal, cast

from langgraph.graph import END, START
from langgraph.graph.state import CompiledStateGraph

from assurance_execution.feature import ExecutionGraphs
from assurance_execution.graphs.nodes import (
    activation_execute,
    activation_rerun,
    publish_execution,
    select_execute,
    select_rerun,
)
from assurance_execution.graphs.state import ExecutionState
from graph_engine.boot.boot import CapabilityBuildContext
from graph_engine.stategraph import AttemptGraph
from graph_engine.stategraph.routing import select_exclusive_route

_EXECUTE_CONTRACT = "assurance.execution.execute"
_RUN_CONTRACT = "assurance.execution.run"
ExecutionSemanticNodeId = Literal["execution.execute", "execution.run"]


def _node(fn: object) -> Callable[..., Any]:
    return cast(Callable[..., Any], fn)


def terminal_committed(state: ExecutionState) -> dict[str, object]:
    del state
    return {}


def terminal_failed(state: ExecutionState) -> dict[str, object]:
    del state
    return {}


_EXECUTION_OTHERWISE = "committed"
_EXECUTION_TABLE: dict[str, Callable[[Mapping[str, object]], bool]] = {
    "failed": lambda state: bool(state.get("attempt_failure")),
}


def route_execution(state: Mapping[str, object]) -> str:
    named = {target: target if predicate(state) else None for target, predicate in _EXECUTION_TABLE.items()}
    return select_exclusive_route(named, otherwise=_EXECUTION_OTHERWISE)


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
    builder: AttemptGraph[ExecutionState] = AttemptGraph(
        ExecutionState,
        context,
        namespace="execution",
        activation=activation,
    )
    builder.add_attempt(
        semantic_node_id,
        contract_id,
        select=select,
        publish=partial(publish_execution, semantic_node_id=semantic_node_id),
        semantic_node_id=semantic_node_id,
    )
    builder.add_node("committed", _node(terminal_committed))
    builder.add_node("failed", _node(terminal_failed))
    builder.add_edge(START, semantic_node_id)
    builder.add_route(
        semantic_node_id,
        route_execution,
        targets=(*_EXECUTION_TABLE, _EXECUTION_OTHERWISE),
    )
    builder.add_edge("committed", END)
    builder.add_edge("failed", END)
    return builder.compile_subgraph()


__all__ = [
    "ExecutionGraphs",
    "build_execution_graphs",
    "route_execution",
    "terminal_committed",
    "terminal_failed",
]
