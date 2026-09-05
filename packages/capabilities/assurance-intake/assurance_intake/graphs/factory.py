from __future__ import annotations

from collections.abc import Callable, Hashable
from dataclasses import dataclass
from typing import Any, cast

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from assurance_intake.graphs.case import build_case_graph
from assurance_intake.graphs.nodes import (
    activation_case_design,
    activation_case_design_repair,
    activation_case_review,
    activation_one_shot,
    publish_artifacts,
    publish_case_design,
    publish_case_review,
    publish_plan,
    select_case_design,
    select_case_design_repair,
    select_case_review,
    select_explore,
    select_intake,
    select_load_plan,
    select_resolve_plan,
    terminal_done,
    terminal_failed,
)
from assurance_intake.graphs.prepare import build_prepare_graph
from assurance_intake.graphs.routes import route_case_design, route_case_design_repair
from assurance_intake.graphs.state import IntakeState
from graph_engine.boot.boot import CapabilityBuildContext

_INTAKE_ID = "assurance.intake.agent.intake.v1"
_EXPLORE_ID = "assurance.intake.agent.explore.v1"
_CASE_DESIGN_ID = "assurance.intake.agent.case-design.v1"
_CASE_REVIEW_ID = "assurance.intake.agent.case-review.v1"
_RESOLVE_PLAN_ID = "assurance.intake.task.resolve-plan"
_LOAD_PLAN_ID = "assurance.intake.task.load-plan"
_CASE_DESIGN_PATHS: dict[Hashable, str] = {
    "done": "done",
    "case-design-repair": "intake.case-design-repair",
    "failed": "failed",
}


@dataclass(frozen=True, slots=True)
class IntakeGraphs:
    prepare: CompiledStateGraph
    load_plan: CompiledStateGraph
    case: CompiledStateGraph


def build_intake_graphs(context: CapabilityBuildContext) -> IntakeGraphs:
    intake = _compile_leaf(
        context,
        contract_id=_INTAKE_ID,
        semantic_node_id="intake.intake",
        activation=activation_one_shot,
        select=select_intake,
        publish=publish_artifacts,
    )
    explore = _compile_leaf(
        context,
        contract_id=_EXPLORE_ID,
        semantic_node_id="intake.explore",
        activation=activation_one_shot,
        select=select_explore,
        publish=publish_artifacts,
    )
    resolve_plan = _compile_leaf(
        context,
        contract_id=_RESOLVE_PLAN_ID,
        semantic_node_id="intake.resolve-plan",
        activation=activation_one_shot,
        select=select_resolve_plan,
        publish=publish_plan,
    )
    load_plan = _compile_leaf(
        context,
        contract_id=_LOAD_PLAN_ID,
        semantic_node_id="intake.load-plan",
        activation=activation_one_shot,
        select=select_load_plan,
        publish=publish_plan,
    )
    case_design = _compile_case_design(context)
    case_review = _compile_leaf(
        context,
        contract_id=_CASE_REVIEW_ID,
        semantic_node_id="intake.case-review",
        activation=activation_case_review,
        select=select_case_review,
        publish=publish_case_review,
    )
    return IntakeGraphs(
        prepare=build_prepare_graph(
            context,
            intake=intake,
            explore=explore,
            resolve_plan=resolve_plan,
        ),
        load_plan=load_plan,
        case=build_case_graph(context, case_design=case_design, case_review=case_review),
    )


def _compile_leaf(
    context: CapabilityBuildContext,
    *,
    contract_id: str,
    semantic_node_id: str,
    activation: object,
    select: object,
    publish: object,
) -> CompiledStateGraph:
    builder: StateGraph[IntakeState] = StateGraph(IntakeState)
    builder.add_node(
        semantic_node_id,
        cast(
            Callable[..., Any],
            context.attempt(
                contract_id,
                semantic_node_id=semantic_node_id,
                activation=activation,
                select=select,
                publish=publish,
            ),
        ),
    )
    builder.add_edge(START, semantic_node_id)
    builder.add_edge(semantic_node_id, END)
    return context.compile_subgraph(builder)


def _compile_case_design(context: CapabilityBuildContext) -> CompiledStateGraph:
    builder: StateGraph[IntakeState] = StateGraph(IntakeState)
    builder.add_node(
        "intake.case-design",
        cast(
            Callable[..., Any],
            context.attempt(
                _CASE_DESIGN_ID,
                semantic_node_id="intake.case-design",
                activation=activation_case_design,
                select=select_case_design,
                publish=publish_case_design,
            ),
        ),
    )
    builder.add_node(
        "intake.case-design-repair",
        cast(
            Callable[..., Any],
            context.attempt(
                _CASE_DESIGN_ID,
                semantic_node_id="intake.case-design-repair",
                activation=activation_case_design_repair,
                select=select_case_design_repair,
                publish=publish_case_design,
            ),
        ),
    )
    builder.add_node("done", cast(Callable[..., Any], terminal_done))
    builder.add_node("failed", cast(Callable[..., Any], terminal_failed))
    builder.add_edge(START, "intake.case-design")
    builder.add_conditional_edges(
        "intake.case-design",
        cast(Callable[..., Any], route_case_design),
        _CASE_DESIGN_PATHS,
    )
    builder.add_conditional_edges(
        "intake.case-design-repair",
        cast(Callable[..., Any], route_case_design_repair),
        {"done": "done", "failed": "failed"},
    )
    builder.add_edge("done", END)
    builder.add_edge("failed", END)
    return context.compile_subgraph(builder)


__all__ = ["IntakeGraphs", "build_intake_graphs"]
