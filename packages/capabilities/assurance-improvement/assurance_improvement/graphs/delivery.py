from __future__ import annotations

from collections.abc import Callable, Hashable
from typing import Any, cast

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from graph_engine.boot.boot import CapabilityBuildContext

from assurance_improvement.graphs.nodes import (
    activation_one_shot,
    apply_human_interrupt,
    publish_apply_memory,
    publish_archive,
    publish_auto_review,
    publish_evaluate,
    publish_export,
    publish_human_review,
    publish_review,
    publish_rollback,
    select_apply_memory,
    select_auto_review,
    select_evaluate,
    select_export,
    select_human_review,
    select_rollback,
    select_skill,
    terminal_done,
    terminal_failed,
    terminal_rejected,
    terminal_rework,
    terminal_superseded,
)
from assurance_improvement.graphs.routes import (
    route_apply_evaluate,
    route_auto_review,
    route_committed,
    route_human_action,
    route_human_review_result,
)
from assurance_improvement.graphs.state import ImprovementState

_ARCHIVE_ID = "assurance.improvement.agent.archive.v1"
_REVIEW_ID = "assurance.improvement.agent.improvement-review.v1"
_AUTO_REVIEW_ID = "assurance.improvement.task.apply-improvement-auto-review"
_HUMAN_REVIEW_ID = "assurance.improvement.task.apply-improvement-review"
_EVALUATE_ID = "assurance.improvement.task.evaluate-memory-improvement"
_EXPORT_ID = "assurance.improvement.task.export-change-improvement"
_APPLY_ID = "assurance.improvement.task.apply-memory-improvement"
_ROLLBACK_ID = "assurance.improvement.task.rollback-memory-improvement"
_COMMITTED_PATHS: dict[Hashable, str] = {"done": "done", "failed": "failed"}
_AUTO_PATHS: dict[Hashable, str] = {
    "evaluate": "improvement.apply-evaluate",
    "rework": "rework",
    "rejected": "rejected",
    "human-review": "human-review",
    "failed": "failed",
}
_HUMAN_ACTION_PATHS: dict[Hashable, str] = {
    "apply-human-review": "improvement.apply-human-review",
    "failed": "failed",
}
_HUMAN_RESULT_PATHS: dict[Hashable, str] = {
    "evaluate": "improvement.apply-evaluate",
    "rejected": "rejected",
    "rework": "rework",
    "superseded": "superseded",
    "failed": "failed",
}
_EVALUATE_PATHS: dict[Hashable, str] = {"apply": "improvement.apply", "failed": "failed"}


def _attempt(
    context: CapabilityBuildContext,
    contract_id: str,
    semantic_node_id: str,
    select: object,
    publish: object,
) -> Any:
    return context.attempt(
        contract_id,
        semantic_node_id=semantic_node_id,
        activation=activation_one_shot,
        select=select,
        publish=publish,
    )


def _add_shared_terminals(builder: StateGraph[ImprovementState]) -> None:
    builder.add_node("done", cast(Callable[..., Any], terminal_done))
    builder.add_node("failed", cast(Callable[..., Any], terminal_failed))
    builder.add_edge("done", END)
    builder.add_edge("failed", END)


def _build_one_shot(
    context: CapabilityBuildContext,
    *,
    contract_id: str,
    semantic_node_id: str,
    select: object,
    publish: object,
) -> CompiledStateGraph:
    builder: StateGraph[ImprovementState] = StateGraph(ImprovementState)
    builder.add_node(
        semantic_node_id,
        cast(Callable[..., Any], _attempt(context, contract_id, semantic_node_id, select, publish)),
    )
    _add_shared_terminals(builder)
    builder.add_edge(START, semantic_node_id)
    builder.add_conditional_edges(
        semantic_node_id, cast(Callable[..., Any], route_committed), _COMMITTED_PATHS
    )
    return context.compile_subgraph(builder)


def build_archive_graph(context: CapabilityBuildContext) -> CompiledStateGraph:
    return _build_one_shot(
        context,
        contract_id=_ARCHIVE_ID,
        semantic_node_id="improvement.archive",
        select=select_skill,
        publish=publish_archive,
    )


def build_review_graph(context: CapabilityBuildContext) -> CompiledStateGraph:
    return _build_one_shot(
        context,
        contract_id=_REVIEW_ID,
        semantic_node_id="improvement.review",
        select=select_skill,
        publish=publish_review,
    )


def build_evaluate_graph(context: CapabilityBuildContext) -> CompiledStateGraph:
    return _build_one_shot(
        context,
        contract_id=_EVALUATE_ID,
        semantic_node_id="improvement.evaluate",
        select=select_evaluate,
        publish=publish_evaluate,
    )


def build_export_graph(context: CapabilityBuildContext) -> CompiledStateGraph:
    return _build_one_shot(
        context,
        contract_id=_EXPORT_ID,
        semantic_node_id="improvement.export",
        select=select_export,
        publish=publish_export,
    )


def build_rollback_graph(context: CapabilityBuildContext) -> CompiledStateGraph:
    return _build_one_shot(
        context,
        contract_id=_ROLLBACK_ID,
        semantic_node_id="improvement.rollback",
        select=select_rollback,
        publish=publish_rollback,
    )


def build_apply_graph(context: CapabilityBuildContext) -> CompiledStateGraph:
    builder: StateGraph[ImprovementState] = StateGraph(ImprovementState)
    builder.add_node(
        "improvement.apply-auto-review",
        cast(
            Callable[..., Any],
            _attempt(
                context,
                _AUTO_REVIEW_ID,
                "improvement.apply-auto-review",
                select_auto_review,
                publish_auto_review,
            ),
        ),
    )
    builder.add_node("human-review", cast(Callable[..., Any], apply_human_interrupt))
    builder.add_node(
        "improvement.apply-human-review",
        cast(
            Callable[..., Any],
            _attempt(
                context,
                _HUMAN_REVIEW_ID,
                "improvement.apply-human-review",
                select_human_review,
                publish_human_review,
            ),
        ),
    )
    builder.add_node(
        "improvement.apply-evaluate",
        cast(
            Callable[..., Any],
            _attempt(context, _EVALUATE_ID, "improvement.apply-evaluate", select_evaluate, publish_evaluate),
        ),
    )
    builder.add_node(
        "improvement.apply",
        cast(
            Callable[..., Any],
            _attempt(context, _APPLY_ID, "improvement.apply", select_apply_memory, publish_apply_memory),
        ),
    )
    builder.add_node("rejected", cast(Callable[..., Any], terminal_rejected))
    builder.add_node("rework", cast(Callable[..., Any], terminal_rework))
    builder.add_node("superseded", cast(Callable[..., Any], terminal_superseded))
    _add_shared_terminals(builder)
    builder.add_edge(START, "improvement.apply-auto-review")
    builder.add_conditional_edges(
        "improvement.apply-auto-review",
        cast(Callable[..., Any], route_auto_review),
        _AUTO_PATHS,
    )
    builder.add_conditional_edges(
        "human-review", cast(Callable[..., Any], route_human_action), _HUMAN_ACTION_PATHS
    )
    builder.add_conditional_edges(
        "improvement.apply-human-review",
        cast(Callable[..., Any], route_human_review_result),
        _HUMAN_RESULT_PATHS,
    )
    builder.add_conditional_edges(
        "improvement.apply-evaluate",
        cast(Callable[..., Any], route_apply_evaluate),
        _EVALUATE_PATHS,
    )
    builder.add_conditional_edges(
        "improvement.apply", cast(Callable[..., Any], route_committed), _COMMITTED_PATHS
    )
    builder.add_edge("rejected", END)
    builder.add_edge("rework", END)
    builder.add_edge("superseded", END)
    return context.compile_subgraph(builder)


__all__ = [
    "build_apply_graph",
    "build_archive_graph",
    "build_evaluate_graph",
    "build_export_graph",
    "build_review_graph",
    "build_rollback_graph",
]
