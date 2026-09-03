from __future__ import annotations

from collections.abc import Callable, Hashable
from typing import Any, cast

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from graph_engine.boot.boot import CapabilityBuildContext

from assurance_improvement.graphs.nodes import (
    activation_one_shot,
    assemble_analyses,
    publish_collect,
    publish_eval_analysis,
    publish_issue_analysis,
    publish_reconcile,
    publish_retro,
    publish_workflow_analysis,
    select_collect,
    select_eval_analysis,
    select_issue_analysis,
    select_reconcile,
    select_retro,
    select_workflow_analysis,
    terminal_done,
    terminal_failed,
)
from assurance_improvement.graphs.routes import route_committed
from assurance_improvement.graphs.state import ImprovementState

_COLLECT_ID = "assurance.improvement.task.retro-collect-v3"
_RECONCILE_ID = "assurance.improvement.task.reconcile-improvements"
_EVAL_ID = "assurance.improvement.agent.retro-eval-analysis.v1"
_ISSUE_ID = "assurance.improvement.agent.retro-issue-analysis.v1"
_WORKFLOW_ID = "assurance.improvement.agent.retro-workflow-analysis.v1"
_RETRO_ID = "assurance.improvement.agent.retro.v1"
_COMMITTED_PATHS: dict[Hashable, str] = {"done": "done", "failed": "failed"}
_AFTER_COLLECT: dict[Hashable, str] = {"done": "improvement.retro-eval-analysis", "failed": "failed"}
_AFTER_EVAL: dict[Hashable, str] = {"done": "improvement.retro-issue-analysis", "failed": "failed"}
_AFTER_ISSUE: dict[Hashable, str] = {"done": "improvement.retro-workflow-analysis", "failed": "failed"}
_AFTER_WORKFLOW: dict[Hashable, str] = {"done": "assemble", "failed": "failed"}
_AFTER_RECONCILE: dict[Hashable, str] = {"done": "improvement.retro", "failed": "failed"}


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


def build_retro_graph(context: CapabilityBuildContext) -> CompiledStateGraph:
    builder: StateGraph[ImprovementState] = StateGraph(ImprovementState)
    builder.add_node(
        "improvement.retro-collect",
        cast(
            Callable[..., Any],
            _attempt(context, _COLLECT_ID, "improvement.retro-collect", select_collect, publish_collect),
        ),
    )
    builder.add_node(
        "improvement.retro-eval-analysis",
        cast(
            Callable[..., Any],
            _attempt(
                context,
                _EVAL_ID,
                "improvement.retro-eval-analysis",
                select_eval_analysis,
                publish_eval_analysis,
            ),
        ),
    )
    builder.add_node(
        "improvement.retro-issue-analysis",
        cast(
            Callable[..., Any],
            _attempt(
                context,
                _ISSUE_ID,
                "improvement.retro-issue-analysis",
                select_issue_analysis,
                publish_issue_analysis,
            ),
        ),
    )
    builder.add_node(
        "improvement.retro-workflow-analysis",
        cast(
            Callable[..., Any],
            _attempt(
                context,
                _WORKFLOW_ID,
                "improvement.retro-workflow-analysis",
                select_workflow_analysis,
                publish_workflow_analysis,
            ),
        ),
    )
    builder.add_node("assemble", cast(Callable[..., Any], assemble_analyses))
    builder.add_node(
        "improvement.retro-reconcile",
        cast(
            Callable[..., Any],
            _attempt(
                context, _RECONCILE_ID, "improvement.retro-reconcile", select_reconcile, publish_reconcile
            ),
        ),
    )
    builder.add_node(
        "improvement.retro",
        cast(
            Callable[..., Any], _attempt(context, _RETRO_ID, "improvement.retro", select_retro, publish_retro)
        ),
    )
    builder.add_node("done", cast(Callable[..., Any], terminal_done))
    builder.add_node("failed", cast(Callable[..., Any], terminal_failed))
    builder.add_edge(START, "improvement.retro-collect")
    builder.add_conditional_edges(
        "improvement.retro-collect", cast(Callable[..., Any], route_committed), _AFTER_COLLECT
    )
    builder.add_conditional_edges(
        "improvement.retro-eval-analysis", cast(Callable[..., Any], route_committed), _AFTER_EVAL
    )
    builder.add_conditional_edges(
        "improvement.retro-issue-analysis", cast(Callable[..., Any], route_committed), _AFTER_ISSUE
    )
    builder.add_conditional_edges(
        "improvement.retro-workflow-analysis",
        cast(Callable[..., Any], route_committed),
        _AFTER_WORKFLOW,
    )
    builder.add_edge("assemble", "improvement.retro-reconcile")
    builder.add_conditional_edges(
        "improvement.retro-reconcile", cast(Callable[..., Any], route_committed), _AFTER_RECONCILE
    )
    builder.add_conditional_edges(
        "improvement.retro", cast(Callable[..., Any], route_committed), _COMMITTED_PATHS
    )
    builder.add_edge("done", END)
    builder.add_edge("failed", END)
    return context.compile_subgraph(builder)


__all__ = ["build_retro_graph"]
