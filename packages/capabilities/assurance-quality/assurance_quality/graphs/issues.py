from __future__ import annotations

from collections.abc import Callable, Hashable
from typing import Any, Literal, cast

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from graph_engine.boot.boot import CapabilityBuildContext

from assurance_quality.graphs.nodes import (
    activation_one_shot,
    activation_issue_analysis,
    activation_issue_reconcile,
    publish_issue,
    publish_issue_analysis,
    publish_issue_reconcile,
    select_quality,
    select_reconcile_issues,
    terminal_done,
)
from assurance_quality.graphs.routes import route_attempt, route_failure
from assurance_quality.graphs.state import QualityState

_ISSUE_TRIAGE_ID = "assurance.quality.agent.issue-triage.v1"
_ISSUE_ANALYSIS_ID = "assurance.quality.agent.issue-analysis.v1"
_ISSUE_RECONCILE_ID = "assurance.quality.reconcile-issues"
_FAILURE_PATHS: dict[Hashable, str] = {
    "fix-eligible": "fix-eligible",
    "report-issue": "report-issue",
    "failed": "failed",
}
_RECONCILE_PATHS: dict[Hashable, str] = {"ready": END, "failed": "failed"}
IssueExport = Literal["issue-review", "issue-analyze", "issue-reconcile"]
_CONTRACT_BY_EXPORT: dict[IssueExport, str] = {
    "issue-review": _ISSUE_TRIAGE_ID,
    "issue-analyze": _ISSUE_ANALYSIS_ID,
    "issue-reconcile": _ISSUE_RECONCILE_ID,
}


def build_issue_graph(context: CapabilityBuildContext, *, export: IssueExport) -> CompiledStateGraph:
    semantic_node_id = f"quality.{export}"
    builder: StateGraph[QualityState] = StateGraph(QualityState)
    if export == "issue-reconcile":
        builder.add_node(
            semantic_node_id,
            cast(
                Callable[..., Any],
                context.attempt(
                    _ISSUE_RECONCILE_ID,
                    semantic_node_id=semantic_node_id,
                    activation=activation_issue_reconcile,
                    select=select_reconcile_issues,
                    publish=publish_issue_reconcile,
                ),
            ),
        )
        builder.add_node("failed", cast(Callable[..., Any], terminal_done))
        builder.add_edge("failed", END)
        builder.add_edge(START, semantic_node_id)
        builder.add_conditional_edges(
            semantic_node_id,
            cast(Callable[..., Any], route_attempt),
            _RECONCILE_PATHS,
        )
        return context.compile_subgraph(builder)
    builder.add_node(
        semantic_node_id,
        cast(
            Callable[..., Any],
            context.attempt(
                _CONTRACT_BY_EXPORT[export],
                semantic_node_id=semantic_node_id,
                activation=activation_one_shot if export == "issue-review" else activation_issue_analysis,
                select=select_quality,
                publish=publish_issue_analysis if export == "issue-analyze" else publish_issue,
            ),
        ),
    )
    for name in _FAILURE_PATHS:
        builder.add_node(str(name), cast(Callable[..., Any], terminal_done))
        builder.add_edge(str(name), END)
    builder.add_edge(START, semantic_node_id)
    builder.add_conditional_edges(
        semantic_node_id,
        cast(Callable[..., Any], route_failure),
        _FAILURE_PATHS,
    )
    return context.compile_subgraph(builder)


__all__ = ["build_issue_graph"]
