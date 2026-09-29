from __future__ import annotations

from collections.abc import Callable, Hashable, Mapping
from typing import Any, cast

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from graph_engine.boot.boot import CapabilityBuildContext
from graph_engine.stategraph import add_attempt_node

from assurance_improvement.graphs.nodes import (
    activation_one_shot,
    assemble_analyses,
    publish_collect,
    publish_build_slices,
    publish_eval_analysis,
    publish_issue_analysis,
    publish_reconcile,
    publish_retro,
    publish_workflow_analysis,
    select_collect,
    select_build_slices,
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
from assurance_improvement.contracts.retro import RetroContextV3

_COLLECT_ID = "assurance.improvement.task.retro-collect-v3"
_BUILD_SLICES_ID = "assurance.improvement.retro-build-slices"
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
_AFTER_RETRO: dict[Hashable, str] = {"done": "improvement.retro-reconcile", "failed": "failed"}


def _route_synthesis(state: ImprovementState) -> str:
    if state.get("analysis_status") != "ok":
        return "failed"
    return route_committed(state)


def _route_assembled(state: Mapping[str, object]) -> str:
    context = RetroContextV3.model_validate(state["context"])
    return "synthesize" if context.signal_count else "empty"


def build_retro_graph(context: CapabilityBuildContext) -> CompiledStateGraph:
    builder: StateGraph[ImprovementState] = StateGraph(ImprovementState)
    add_attempt_node(
        builder,
        context,
        "improvement.retro-build-slices",
        contract_id=_BUILD_SLICES_ID,
        activation=activation_one_shot,
        select=select_build_slices,
        publish=publish_build_slices,
    )
    add_attempt_node(
        builder,
        context,
        "improvement.retro-collect",
        contract_id=_COLLECT_ID,
        activation=activation_one_shot,
        select=select_collect,
        publish=publish_collect,
    )
    add_attempt_node(
        builder,
        context,
        "improvement.retro-eval-analysis",
        contract_id=_EVAL_ID,
        activation=activation_one_shot,
        select=select_eval_analysis,
        publish=publish_eval_analysis,
    )
    add_attempt_node(
        builder,
        context,
        "improvement.retro-issue-analysis",
        contract_id=_ISSUE_ID,
        activation=activation_one_shot,
        select=select_issue_analysis,
        publish=publish_issue_analysis,
    )
    add_attempt_node(
        builder,
        context,
        "improvement.retro-workflow-analysis",
        contract_id=_WORKFLOW_ID,
        activation=activation_one_shot,
        select=select_workflow_analysis,
        publish=publish_workflow_analysis,
    )
    builder.add_node("assemble", cast(Callable[..., Any], assemble_analyses))
    add_attempt_node(
        builder,
        context,
        "improvement.retro-reconcile",
        contract_id=_RECONCILE_ID,
        activation=activation_one_shot,
        select=select_reconcile,
        publish=publish_reconcile,
    )
    add_attempt_node(
        builder,
        context,
        "improvement.retro",
        contract_id=_RETRO_ID,
        activation=activation_one_shot,
        select=select_retro,
        publish=publish_retro,
    )
    builder.add_node("done", cast(Callable[..., Any], terminal_done))
    builder.add_node("failed", cast(Callable[..., Any], terminal_failed))
    builder.add_edge(START, "improvement.retro-build-slices")
    builder.add_conditional_edges(
        "improvement.retro-build-slices",
        cast(Callable[..., Any], route_committed),
        {"done": "improvement.retro-collect", "failed": "failed"},
    )
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
    builder.add_conditional_edges(
        "assemble",
        _route_assembled,
        {"synthesize": "improvement.retro", "empty": "improvement.retro-reconcile"},
    )
    builder.add_conditional_edges(
        "improvement.retro-reconcile", cast(Callable[..., Any], route_committed), _COMMITTED_PATHS
    )
    builder.add_conditional_edges(
        "improvement.retro", cast(Callable[..., Any], _route_synthesis), _AFTER_RETRO
    )
    builder.add_edge("done", END)
    builder.add_edge("failed", END)
    return context.compile_subgraph(builder)


__all__ = ["build_retro_graph"]
