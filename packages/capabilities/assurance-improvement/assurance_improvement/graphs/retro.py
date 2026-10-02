from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, cast

from langgraph.graph import END, START
from langgraph.graph.state import CompiledStateGraph

from graph_engine.boot.boot import CapabilityBuildContext
from graph_engine.stategraph import AttemptGraph

from assurance_improvement.contracts.attempts import TASK_ATTEMPT_CONTRACTS
from assurance_improvement.contracts.retro import RetroContextV3
from assurance_improvement.graphs.delivery import route_committed
from assurance_improvement.graphs.nodes import (
    activation_one_shot,
    assemble_analyses,
    publish_build_slices,
    publish_collect,
    publish_eval_analysis,
    publish_issue_analysis,
    publish_reconcile,
    publish_retro,
    publish_workflow_analysis,
    select_build_slices,
    select_collect,
    select_eval_analysis,
    select_issue_analysis,
    select_reconcile,
    select_retro,
    select_workflow_analysis,
    terminal_done,
    terminal_failed,
)
from assurance_improvement.graphs.state import ImprovementState
from assurance_improvement.ops.retro import op as retro
from assurance_improvement.ops.retro_eval_analysis import op as retro_eval_analysis
from assurance_improvement.ops.retro_issue_analysis import op as retro_issue_analysis
from assurance_improvement.ops.retro_workflow_analysis import op as retro_workflow_analysis

_COLLECT = TASK_ATTEMPT_CONTRACTS["assurance.improvement.retro-collect-v3"]
_BUILD_SLICES = TASK_ATTEMPT_CONTRACTS["assurance.improvement.retro-build-slices"]
_RECONCILE = TASK_ATTEMPT_CONTRACTS["assurance.improvement.reconcile-improvements"]
_FAILED = "failed"
_COMMITTED_TARGETS = ("done", _FAILED)
_RETRO_NODE = "improvement.retro"
_RECONCILE_NODE = "improvement.retro-reconcile"
_ASSEMBLED_TARGETS = (_RETRO_NODE, _RECONCILE_NODE)
_SYNTHESIS_TARGETS = (_RECONCILE_NODE, _FAILED)


def _node(fn: object) -> Callable[..., Any]:
    return cast(Callable[..., Any], fn)


def _route_assembled(state: Mapping[str, object]) -> str:
    context = RetroContextV3.model_validate(state["context"])
    return _RETRO_NODE if context.signal_count else _RECONCILE_NODE


def _route_synthesis(state: Mapping[str, object]) -> str:
    if state.get("analysis_status") != "ok":
        return _FAILED
    return _RECONCILE_NODE if route_committed(state) == "done" else _FAILED


def build_retro_graph(context: CapabilityBuildContext) -> CompiledStateGraph:
    builder: AttemptGraph[ImprovementState] = AttemptGraph(
        ImprovementState,
        context,
        namespace="improvement",
        activation=activation_one_shot,
    )
    builder.add_attempt(
        "improvement.retro-build-slices",
        _BUILD_SLICES,
        select=select_build_slices,
        publish=publish_build_slices,
        semantic_node_id="improvement.retro-build-slices",
    )
    builder.add_attempt(
        "improvement.retro-collect",
        _COLLECT,
        select=select_collect,
        publish=publish_collect,
        semantic_node_id="improvement.retro-collect",
    )
    builder.add_attempt(
        "improvement.retro-eval-analysis",
        retro_eval_analysis,
        select=select_eval_analysis,
        publish=publish_eval_analysis,
        semantic_node_id="improvement.retro-eval-analysis",
    )
    builder.add_attempt(
        "improvement.retro-issue-analysis",
        retro_issue_analysis,
        select=select_issue_analysis,
        publish=publish_issue_analysis,
        semantic_node_id="improvement.retro-issue-analysis",
    )
    builder.add_attempt(
        "improvement.retro-workflow-analysis",
        retro_workflow_analysis,
        select=select_workflow_analysis,
        publish=publish_workflow_analysis,
        semantic_node_id="improvement.retro-workflow-analysis",
    )
    builder.add_node("assemble", _node(assemble_analyses))
    builder.add_attempt(
        "improvement.retro-reconcile",
        _RECONCILE,
        select=select_reconcile,
        publish=publish_reconcile,
        semantic_node_id="improvement.retro-reconcile",
    )
    builder.add_attempt(
        "improvement.retro",
        retro,
        select=select_retro,
        publish=publish_retro,
        semantic_node_id="improvement.retro",
    )
    builder.add_node("done", _node(terminal_done))
    builder.add_node("failed", _node(terminal_failed))
    builder.add_edge(START, "improvement.retro-build-slices")
    builder.add_attempt_edge(
        "improvement.retro-build-slices",
        "improvement.retro-collect",
        on_failure=_FAILED,
    )
    builder.add_attempt_edge(
        "improvement.retro-collect",
        "improvement.retro-eval-analysis",
        on_failure=_FAILED,
    )
    builder.add_attempt_edge(
        "improvement.retro-eval-analysis",
        "improvement.retro-issue-analysis",
        on_failure=_FAILED,
    )
    builder.add_attempt_edge(
        "improvement.retro-issue-analysis",
        "improvement.retro-workflow-analysis",
        on_failure=_FAILED,
    )
    builder.add_attempt_edge(
        "improvement.retro-workflow-analysis",
        "assemble",
        on_failure=_FAILED,
    )
    builder.add_route("assemble", _route_assembled, targets=_ASSEMBLED_TARGETS)
    builder.add_route(
        "improvement.retro-reconcile",
        route_committed,
        targets=_COMMITTED_TARGETS,
    )
    builder.add_route("improvement.retro", _route_synthesis, targets=_SYNTHESIS_TARGETS)
    builder.add_edge("done", END)
    builder.add_edge("failed", END)
    return builder.compile_subgraph()


__all__ = ["build_retro_graph"]
