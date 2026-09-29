from __future__ import annotations

from collections.abc import Callable, Hashable
from typing import Any, cast

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from assurance_generation.graphs.nodes import (
    activation_codegen,
    activation_codegen_review,
    human_review,
    human_review_retry,
    plan_round_join,
    publish_codegen,
    publish_codegen_review,
    review_round_advance,
    review_round_advance_retry,
    select_codegen,
    select_codegen_review,
    terminal_done,
    terminal_exhausted,
    terminal_rejected,
    terminal_skipped,
)
from assurance_generation.graphs.routes import (
    route_attempt_result,
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
from graph_engine.stategraph import add_attempt_node

_CODEGEN_REVIEW_PATHS: dict[Hashable, str] = {
    "done": "done",
    "codegen-review-round-advance": "codegen-review-round-advance",
    "codegen-human-review": "codegen-human-review",
    "rejected": "rejected",
    "exhausted": "exhausted",
    "failed": "done",
}
_CODEGEN_REVIEW_RETRY_PATHS: dict[Hashable, str] = {
    "done": "done",
    "codegen-review-round-advance-retry": "codegen-review-round-advance-retry",
    "codegen-human-review-retry": "codegen-human-review-retry",
    "rejected": "rejected",
    "exhausted": "exhausted",
    "failed": "done",
}
_HUMAN_PATHS: dict[Hashable, str] = {
    "done": "done",
    "rejected": "rejected",
    "codegen-review-round-advance": "codegen-review-round-advance",
    "exhausted": "exhausted",
}
_HUMAN_RETRY_PATHS: dict[Hashable, str] = {
    "done": "done",
    "rejected": "rejected",
    "codegen-review-round-advance-retry": "codegen-review-round-advance-retry",
    "exhausted": "exhausted",
}
_ADVANCE_PATHS: dict[Hashable, str] = {
    "codegen-round-join": "codegen-round-join",
    "exhausted": "exhausted",
}
_ENTRY_PATHS: dict[Hashable, str] = {"codegen": "codegen", "skip": "skip"}
_ATTEMPT_PATHS: dict[Hashable, str] = {"committed": "codegen-review", "failed": "done"}
_RETRY_ATTEMPT_PATHS: dict[Hashable, str] = {
    "committed": "codegen-review-retry",
    "failed": "done",
}


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
    add_attempt_node(
        builder,
        context,
        semantic_node_id,
        contract_id=_contract_id(family, stage),
        activation=activation,
        select=select,
        publish=publish,
    )
    builder.add_edge(START, semantic_node_id)
    builder.add_edge(semantic_node_id, END)
    return context.compile_subgraph(builder)


def _assemble_family_graph(
    context: CapabilityBuildContext,
    *,
    codegen: CompiledStateGraph,
    codegen_review: CompiledStateGraph,
    output_schema: type[FamilyLaneOutput] | type[GenerationState] | None,
) -> CompiledStateGraph:
    builder = (
        StateGraph(GenerationState, output_schema=output_schema)
        if output_schema is not None
        else StateGraph(GenerationState)
    )
    builder.add_node("codegen", codegen)
    builder.add_node("codegen-retry", codegen)
    builder.add_node("codegen-review", codegen_review)
    builder.add_node("codegen-review-retry", codegen_review)
    builder.add_node("codegen-review-round-advance", _node(review_round_advance))
    builder.add_node("codegen-review-round-advance-retry", _node(review_round_advance_retry))
    builder.add_node("codegen-round-join", _node(plan_round_join))
    builder.add_node("codegen-human-review", _node(human_review))
    builder.add_node("codegen-human-review-retry", _node(human_review_retry))
    builder.add_node("skip", _node(terminal_skipped))
    builder.add_node("done", _node(terminal_done))
    builder.add_node("rejected", _node(terminal_rejected))
    builder.add_node("exhausted", _node(terminal_exhausted))
    builder.add_conditional_edges(START, cast(Callable[..., Any], route_family_entry), _ENTRY_PATHS)
    builder.add_conditional_edges(
        "codegen",
        cast(Callable[..., Any], route_attempt_result),
        _ATTEMPT_PATHS,
    )
    builder.add_conditional_edges(
        "codegen-review",
        cast(Callable[..., Any], route_plan_review),
        _CODEGEN_REVIEW_PATHS,
    )
    builder.add_conditional_edges(
        "codegen-human-review",
        cast(Callable[..., Any], route_plan_human_review),
        _HUMAN_PATHS,
    )
    builder.add_conditional_edges(
        "codegen-review-round-advance",
        cast(Callable[..., Any], route_plan_advance),
        _ADVANCE_PATHS,
    )
    builder.add_conditional_edges(
        "codegen-review-round-advance-retry",
        cast(Callable[..., Any], route_plan_advance_retry),
        _ADVANCE_PATHS,
    )
    builder.add_edge("codegen-round-join", "codegen-retry")
    builder.add_conditional_edges(
        "codegen-retry",
        cast(Callable[..., Any], route_attempt_result),
        _RETRY_ATTEMPT_PATHS,
    )
    builder.add_conditional_edges(
        "codegen-review-retry",
        cast(Callable[..., Any], route_plan_review_retry),
        _CODEGEN_REVIEW_RETRY_PATHS,
    )
    builder.add_conditional_edges(
        "codegen-human-review-retry",
        cast(Callable[..., Any], route_plan_human_review_retry),
        _HUMAN_RETRY_PATHS,
    )
    builder.add_edge("skip", END)
    builder.add_edge("done", END)
    builder.add_edge("rejected", END)
    builder.add_edge("exhausted", END)
    return context.compile_subgraph(builder)


def compile_family_pair(
    context: CapabilityBuildContext,
    family: str,
) -> tuple[CompiledStateGraph, CompiledStateGraph]:
    codegen = _compile_leaf(
        context,
        family=family,
        stage="codegen",
        activation=activation_codegen,
        select=select_codegen,
        publish=publish_codegen,
    )
    codegen_review = _compile_leaf(
        context,
        family=family,
        stage="codegen-review",
        activation=activation_codegen_review,
        select=select_codegen_review,
        publish=publish_codegen_review,
    )
    leaves = {
        "codegen": codegen,
        "codegen_review": codegen_review,
    }
    standalone = _assemble_family_graph(context, output_schema=None, **leaves)
    lane = _assemble_family_graph(context, output_schema=FamilyLaneOutput, **leaves)
    return standalone, lane


def compile_family_graph(
    context: CapabilityBuildContext,
    family: str,
) -> CompiledStateGraph:
    return compile_family_pair(context, family)[0]


def build_api_graph(context: CapabilityBuildContext) -> CompiledStateGraph:
    return compile_family_graph(context, "api")


__all__ = ["build_api_graph", "compile_family_graph", "compile_family_pair"]
