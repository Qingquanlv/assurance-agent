from __future__ import annotations

from collections.abc import Callable, Hashable
from typing import Any, cast

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from assurance_generation.graphs.nodes import (
    activation_codegen,
    activation_plan,
    activation_plan_review,
    codegen_round_advance,
    human_review,
    human_review_retry,
    plan_round_join,
    publish_codegen,
    publish_plan,
    publish_plan_review,
    review_round_advance,
    review_round_advance_retry,
    select_codegen,
    select_codegen_fix,
    select_plan,
    select_plan_review,
    terminal_done,
    terminal_exhausted,
    terminal_rejected,
    terminal_skipped,
)
from assurance_generation.graphs.routes import (
    route_codegen,
    route_family_entry,
    route_plan_advance,
    route_plan_advance_retry,
    route_plan_human_review,
    route_plan_human_review_retry,
    route_plan_review,
    route_plan_review_retry,
)
from assurance_generation.graphs.state import FamilyLaneOutput, GenerationState
from graph_engine.boot.boot import CapabilityBuildContext

_PLAN_REVIEW_PATHS: dict[Hashable, str] = {
    "codegen": "codegen",
    "plan-review-round-advance": "plan-review-round-advance",
    "plan-human-review": "plan-human-review",
    "rejected": "rejected",
    "exhausted": "exhausted",
}
_PLAN_REVIEW_RETRY_PATHS: dict[Hashable, str] = {
    "codegen": "codegen",
    "plan-review-round-advance-retry": "plan-review-round-advance-retry",
    "plan-human-review-retry": "plan-human-review-retry",
    "rejected": "rejected",
    "exhausted": "exhausted",
}
_HUMAN_PATHS: dict[Hashable, str] = {
    "codegen": "codegen",
    "rejected": "rejected",
    "plan-review-round-advance": "plan-review-round-advance",
    "exhausted": "exhausted",
}
_HUMAN_RETRY_PATHS: dict[Hashable, str] = {
    "codegen": "codegen",
    "rejected": "rejected",
    "plan-review-round-advance-retry": "plan-review-round-advance-retry",
    "exhausted": "exhausted",
}
_ADVANCE_PATHS: dict[Hashable, str] = {
    "plan-round-join": "plan-round-join",
    "exhausted": "exhausted",
}
_CODEGEN_PATHS: dict[Hashable, str] = {
    "done": "done",
    "codegen-round-advance": "codegen-round-advance",
    "exhausted": "exhausted",
}
_ENTRY_PATHS: dict[Hashable, str] = {"plan": "plan", "skip": "skip"}


def _node(fn: object) -> Callable[..., Any]:
    return cast(Callable[..., Any], fn)


def _contract_id(family: str, stage: str) -> str:
    return f"assurance.generation.agent.{family}.{stage}.v1"


def _compile_leaf(
    context: CapabilityBuildContext,
    *,
    family: str,
    stage: str,
    activation: object,
    select: object,
    publish: object,
) -> CompiledStateGraph:
    semantic_node_id = f"generation.{family}.{stage}"
    builder: StateGraph[GenerationState] = StateGraph(GenerationState)
    builder.add_node(
        semantic_node_id,
        cast(
            Callable[..., Any],
            context.attempt(
                _contract_id(family, stage),
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


def _assemble_family_graph(
    context: CapabilityBuildContext,
    *,
    has_codegen_fix: bool,
    plan: CompiledStateGraph,
    plan_review: CompiledStateGraph,
    codegen: CompiledStateGraph,
    codegen_fix: CompiledStateGraph | None,
    output_schema: type[FamilyLaneOutput] | type[GenerationState] | None,
) -> CompiledStateGraph:
    builder = (
        StateGraph(GenerationState, output_schema=output_schema)
        if output_schema is not None
        else StateGraph(GenerationState)
    )
    builder.add_node("plan", plan)
    builder.add_node("plan-retry", plan)
    builder.add_node("plan-review", plan_review)
    builder.add_node("plan-review-retry", plan_review)
    builder.add_node("codegen", codegen)
    if has_codegen_fix:
        assert codegen_fix is not None
        builder.add_node("codegen-fix", codegen_fix)
        builder.add_node("codegen-round-advance", _node(codegen_round_advance))
    builder.add_node("plan-review-round-advance", _node(review_round_advance))
    builder.add_node("plan-review-round-advance-retry", _node(review_round_advance_retry))
    builder.add_node("plan-round-join", _node(plan_round_join))
    builder.add_node("plan-human-review", _node(human_review))
    builder.add_node("plan-human-review-retry", _node(human_review_retry))
    builder.add_node("skip", _node(terminal_skipped))
    builder.add_node("done", _node(terminal_done))
    builder.add_node("rejected", _node(terminal_rejected))
    builder.add_node("exhausted", _node(terminal_exhausted))
    builder.add_conditional_edges(START, cast(Callable[..., Any], route_family_entry), _ENTRY_PATHS)
    builder.add_edge("plan", "plan-review")
    builder.add_conditional_edges(
        "plan-review",
        cast(Callable[..., Any], route_plan_review),
        _PLAN_REVIEW_PATHS,
    )
    builder.add_conditional_edges(
        "plan-human-review",
        cast(Callable[..., Any], route_plan_human_review),
        _HUMAN_PATHS,
    )
    builder.add_conditional_edges(
        "plan-review-round-advance",
        cast(Callable[..., Any], route_plan_advance),
        _ADVANCE_PATHS,
    )
    builder.add_conditional_edges(
        "plan-review-round-advance-retry",
        cast(Callable[..., Any], route_plan_advance_retry),
        _ADVANCE_PATHS,
    )
    builder.add_edge("plan-round-join", "plan-retry")
    builder.add_edge("plan-retry", "plan-review-retry")
    builder.add_conditional_edges(
        "plan-review-retry",
        cast(Callable[..., Any], route_plan_review_retry),
        _PLAN_REVIEW_RETRY_PATHS,
    )
    builder.add_conditional_edges(
        "plan-human-review-retry",
        cast(Callable[..., Any], route_plan_human_review_retry),
        _HUMAN_RETRY_PATHS,
    )
    if has_codegen_fix:
        builder.add_conditional_edges(
            "codegen",
            cast(Callable[..., Any], route_codegen),
            _CODEGEN_PATHS,
        )
        builder.add_edge("codegen-round-advance", "codegen-fix")
        builder.add_edge("codegen-fix", "done")
    else:
        builder.add_edge("codegen", "done")
    builder.add_edge("skip", END)
    builder.add_edge("done", END)
    builder.add_edge("rejected", END)
    builder.add_edge("exhausted", END)
    return context.compile_subgraph(builder)


def compile_family_pair(
    context: CapabilityBuildContext,
    family: str,
    *,
    has_codegen_fix: bool,
) -> tuple[CompiledStateGraph, CompiledStateGraph]:
    plan = _compile_leaf(
        context,
        family=family,
        stage="plan",
        activation=activation_plan,
        select=select_plan,
        publish=publish_plan,
    )
    plan_review = _compile_leaf(
        context,
        family=family,
        stage="plan-review",
        activation=activation_plan_review,
        select=select_plan_review,
        publish=publish_plan_review,
    )
    codegen = _compile_leaf(
        context,
        family=family,
        stage="codegen",
        activation=activation_codegen,
        select=select_codegen,
        publish=publish_codegen,
    )
    codegen_fix = None
    if has_codegen_fix:
        codegen_fix = _compile_leaf(
            context,
            family=family,
            stage="codegen-fix",
            activation=activation_codegen,
            select=select_codegen_fix,
            publish=publish_codegen,
        )
    leaves = {
        "has_codegen_fix": has_codegen_fix,
        "plan": plan,
        "plan_review": plan_review,
        "codegen": codegen,
        "codegen_fix": codegen_fix,
    }
    standalone = _assemble_family_graph(context, output_schema=None, **leaves)
    lane = _assemble_family_graph(context, output_schema=FamilyLaneOutput, **leaves)
    return standalone, lane


def compile_family_graph(
    context: CapabilityBuildContext,
    family: str,
    *,
    has_codegen_fix: bool,
) -> CompiledStateGraph:
    return compile_family_pair(context, family, has_codegen_fix=has_codegen_fix)[0]


def build_api_graph(context: CapabilityBuildContext) -> CompiledStateGraph:
    return compile_family_graph(context, "api", has_codegen_fix=True)


__all__ = ["build_api_graph", "compile_family_graph", "compile_family_pair"]
