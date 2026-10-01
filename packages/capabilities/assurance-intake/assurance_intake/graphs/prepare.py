from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from assurance_intake.graphs.calls import (
    activation_one_shot,
    publish_artifacts,
    publish_plan,
    select_explore,
    select_intake,
    select_resolve_plan,
)
from assurance_intake.graphs.state import IntakeState, terminal_failed, terminal_prepared
from assurance_intake.ops.explore import op as explore
from assurance_intake.ops.intake import op as intake
from assurance_intake.ops.resolve_plan import op as resolve_plan
from graph_engine.boot.boot import CapabilityBuildContext
from graph_engine.stategraph import add_attempt_edge, add_attempt_node


def _node(fn: object) -> Callable[..., Any]:
    return cast(Callable[..., Any], fn)


def build_prepare_graph(context: CapabilityBuildContext) -> CompiledStateGraph:
    builder: StateGraph[IntakeState] = StateGraph(IntakeState)
    add_attempt_node(
        builder,
        context,
        "intake",
        semantic_node_id="intake.intake",
        contract_id=intake.contract_id,
        activation=activation_one_shot,
        select=select_intake,
        publish=publish_artifacts,
    )
    add_attempt_node(
        builder,
        context,
        "explore",
        semantic_node_id="intake.explore",
        contract_id=explore.contract_id,
        activation=activation_one_shot,
        select=select_explore,
        publish=publish_artifacts,
    )
    add_attempt_node(
        builder,
        context,
        "resolve-plan",
        semantic_node_id="intake.resolve-plan",
        contract_id=resolve_plan.contract_id,
        activation=activation_one_shot,
        select=select_resolve_plan,
        publish=publish_plan,
    )
    builder.add_node("prepared", _node(terminal_prepared))
    builder.add_node("failed", _node(terminal_failed))
    builder.add_edge(START, "intake")
    add_attempt_edge(builder, "intake", "explore", on_failure="failed")
    add_attempt_edge(builder, "explore", "resolve-plan", on_failure="failed")
    add_attempt_edge(builder, "resolve-plan", "prepared", on_failure="failed")
    builder.add_edge("prepared", END)
    builder.add_edge("failed", END)
    return context.compile_subgraph(builder)


__all__ = ["build_prepare_graph"]
