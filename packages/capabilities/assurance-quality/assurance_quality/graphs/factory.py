from __future__ import annotations

from dataclasses import dataclass

from langgraph.graph.state import CompiledStateGraph

from graph_engine.boot.boot import CapabilityBuildContext

from assurance_quality.graphs.assessment import build_assess_graph
from assurance_quality.graphs.issues import build_issue_graph
from assurance_quality.graphs.report import build_report_graph


@dataclass(frozen=True, slots=True)
class QualityGraphs:
    assess: CompiledStateGraph
    issue_review: CompiledStateGraph
    issue_analyze: CompiledStateGraph
    issue_reconcile: CompiledStateGraph
    report: CompiledStateGraph


def build_quality_graphs(context: CapabilityBuildContext) -> QualityGraphs:
    return QualityGraphs(
        assess=build_assess_graph(context),
        issue_review=build_issue_graph(context, export="issue-review"),
        issue_analyze=build_issue_graph(context, export="issue-analyze"),
        issue_reconcile=build_issue_graph(context, export="issue-reconcile"),
        report=build_report_graph(context),
    )


__all__ = ["QualityGraphs", "build_quality_graphs"]
