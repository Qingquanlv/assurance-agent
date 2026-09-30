from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from assurance_generation.graphs.nodes import (
    activation_codegen,
    activation_codegen_review,
    human_review,
    plan_round_join,
    publish_codegen,
    publish_codegen_review,
    review_round_advance,
    select_codegen,
    select_codegen_review,
    terminal_done,
    terminal_exhausted,
    terminal_rejected,
    terminal_skipped,
)
from assurance_generation.graphs.routes import (
    route_family_entry,
    route_plan_advance,
    route_plan_human_review,
    route_plan_review,
)
from assurance_generation.graphs.state import FamilyLaneOutput, GenerationState
from graph_engine.boot.boot import CapabilityBuildContext
from graph_engine.stategraph import add_attempt_edge, add_attempt_node, add_route


def _node(fn: object) -> Callable[..., Any]:
    return cast(Callable[..., Any], fn)


def _contract_id(family: str, stage: str) -> str:
    return f"assurance.generation.agent.{family}.{stage}.v1"


def _assemble_family_graph(
    context: CapabilityBuildContext,
    *,
    family: str,
    output_schema: type[FamilyLaneOutput] | type[GenerationState] | None,
) -> CompiledStateGraph:
    builder = (
        StateGraph(GenerationState, output_schema=output_schema)
        if output_schema is not None
        else StateGraph(GenerationState)
    )
    add_attempt_node(
        builder,
        context,
        "codegen",
        semantic_node_id=f"generation.{family}.codegen",
        contract_id=_contract_id(family, "codegen"),
        activation=activation_codegen,
        select=select_codegen,
        publish=publish_codegen,
    )
    add_attempt_node(
        builder,
        context,
        "codegen-review",
        semantic_node_id=f"generation.{family}.codegen-review",
        contract_id=_contract_id(family, "codegen-review"),
        activation=activation_codegen_review,
        select=select_codegen_review,
        publish=publish_codegen_review,
    )
    builder.add_node("codegen-review-round-advance", _node(review_round_advance))
    builder.add_node("codegen-round-join", _node(plan_round_join))
    builder.add_node("codegen-human-review", _node(human_review))
    builder.add_node("skip", _node(terminal_skipped))
    builder.add_node("done", _node(terminal_done))
    builder.add_node("rejected", _node(terminal_rejected))
    builder.add_node("exhausted", _node(terminal_exhausted))
    add_route(builder, START, route_family_entry, targets=("codegen", "skip"))
    add_attempt_edge(builder, "codegen", "codegen-review", on_failure="done")
    add_route(
        builder,
        "codegen-review",
        route_plan_review,
        targets=("done", "codegen-review-round-advance", "codegen-human-review", "rejected", "exhausted"),
        on_failure="done",
    )
    add_route(
        builder,
        "codegen-human-review",
        route_plan_human_review,
        targets=("done", "rejected", "codegen-review-round-advance", "exhausted"),
    )
    add_route(
        builder,
        "codegen-review-round-advance",
        route_plan_advance,
        targets=("codegen-round-join", "exhausted"),
    )
    builder.add_edge("codegen-round-join", "codegen")
    builder.add_edge("skip", END)
    builder.add_edge("done", END)
    builder.add_edge("rejected", END)
    builder.add_edge("exhausted", END)
    return context.compile_subgraph(builder)


def compile_family_pair(
    context: CapabilityBuildContext,
    family: str,
) -> tuple[CompiledStateGraph, CompiledStateGraph]:
    standalone = _assemble_family_graph(context, family=family, output_schema=None)
    lane = _assemble_family_graph(context, family=family, output_schema=FamilyLaneOutput)
    return standalone, lane


def compile_family_graph(
    context: CapabilityBuildContext,
    family: str,
) -> CompiledStateGraph:
    return compile_family_pair(context, family)[0]


def build_api_graph(context: CapabilityBuildContext) -> CompiledStateGraph:
    return compile_family_graph(context, "api")


__all__ = ["build_api_graph", "compile_family_graph", "compile_family_pair"]
