from __future__ import annotations

from collections.abc import Callable, Hashable, Mapping
from typing import Any, Literal, cast

from langgraph.graph import END, START
from langgraph.graph.state import CompiledStateGraph

from graph_engine.boot.boot import CapabilityBuildContext
from graph_engine.stategraph import AttemptGraph
from graph_engine.stategraph.routing import select_exclusive_route

from assurance_quality.contracts.decisions import FIX_ELIGIBLE_CLASSIFICATIONS
from assurance_quality.graphs.nodes import (
    activation_issue_analysis,
    activation_issue_reconcile,
    activation_one_shot,
    publish_issue,
    publish_issue_analysis,
    publish_issue_reconcile,
    select_quality,
    select_reconcile_issues,
    terminal_done,
)
from assurance_quality.graphs.state import QualityState

_ISSUE_TRIAGE_ID = "assurance.quality.agent.issue-triage.v1"
_ISSUE_ANALYSIS_ID = "assurance.quality.agent.issue-analysis.v1"
_ISSUE_RECONCILE_ID = "assurance.quality.reconcile-issues"
_FAILURE_OTHERWISE = "failed"
_FAILURE_TARGETS = ("fix-eligible", "report-issue", _FAILURE_OTHERWISE)
_RECONCILE_PATHS: dict[Hashable, str] = {"ready": END, "failed": "failed"}
IssueExport = Literal["issue-review", "issue-analyze", "issue-reconcile"]
_CONTRACT_BY_EXPORT: dict[IssueExport, str] = {
    "issue-review": _ISSUE_TRIAGE_ID,
    "issue-analyze": _ISSUE_ANALYSIS_ID,
    "issue-reconcile": _ISSUE_RECONCILE_ID,
}


def route_attempt(success: str) -> Callable[[Mapping[str, object]], str]:
    def route(state: Mapping[str, object]) -> str:
        return "failed" if state.get("attempt_failure") else success

    route.__name__ = "route_attempt"
    return route


def failure_named_matches(state: Mapping[str, object]) -> dict[str, str | None]:
    classification = state.get("classification")
    eligible = state.get("fix_eligible") is True
    return {
        "fix_eligible": (
            "fix-eligible" if classification in FIX_ELIGIBLE_CLASSIFICATIONS and eligible else None
        ),
        "product_bug": "report-issue" if classification == "product_bug" else None,
        "environment_failure": "report-issue" if classification == "environment_failure" else None,
        "infrastructure_failure": "report-issue" if classification == "infrastructure_failure" else None,
    }


def route_failure(state: Mapping[str, object]) -> str:
    if state.get("attempt_failure"):
        return _FAILURE_OTHERWISE
    return select_exclusive_route(failure_named_matches(state), otherwise=_FAILURE_OTHERWISE)


def _node(fn: object) -> Callable[..., Any]:
    return cast(Callable[..., Any], fn)


def build_issue_graph(context: CapabilityBuildContext, *, export: IssueExport) -> CompiledStateGraph:
    semantic_node_id = f"quality.{export}"
    builder: AttemptGraph[QualityState] = AttemptGraph(QualityState, context, namespace="quality")
    if export == "issue-reconcile":
        builder.add_attempt(
            semantic_node_id,
            _ISSUE_RECONCILE_ID,
            select=select_reconcile_issues,
            publish=publish_issue_reconcile,
            activation=activation_issue_reconcile,
            semantic_node_id=semantic_node_id,
        )
        builder.add_node("failed", _node(terminal_done))
        builder.add_edge("failed", END)
        builder.add_edge(START, semantic_node_id)
        builder.add_conditional_edges(
            semantic_node_id,
            _node(route_attempt("ready")),
            _RECONCILE_PATHS,
        )
        return builder.compile_subgraph()
    builder.add_attempt(
        semantic_node_id,
        _CONTRACT_BY_EXPORT[export],
        select=select_quality,
        publish=publish_issue_analysis if export == "issue-analyze" else publish_issue,
        activation=activation_one_shot if export == "issue-review" else activation_issue_analysis,
        semantic_node_id=semantic_node_id,
    )
    for name in _FAILURE_TARGETS:
        builder.add_node(name, _node(terminal_done))
        builder.add_edge(name, END)
    builder.add_edge(START, semantic_node_id)
    builder.add_route(semantic_node_id, route_failure, targets=_FAILURE_TARGETS)
    return builder.compile_subgraph()


__all__ = [
    "build_issue_graph",
    "failure_named_matches",
    "route_attempt",
    "route_failure",
]
