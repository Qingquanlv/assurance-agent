from __future__ import annotations

from graph_engine.boot.boot import CapabilityBuildContext

from assurance_quality.feature import QualityGraphs
from assurance_quality.graphs.assessment import build_assess_graph
from assurance_quality.graphs.fact_baseline import build_fact_baseline_graph
from assurance_quality.graphs.issues import build_issue_graph
from assurance_quality.graphs.report import build_report_graph
from assurance_quality.graphs.surface import build_surface_baseline_graph


def build_quality_graphs(context: CapabilityBuildContext) -> QualityGraphs:
    return QualityGraphs(
        assess=build_assess_graph(context),
        issue_review=build_issue_graph(context, export="issue-review"),
        issue_analyze=build_issue_graph(context, export="issue-analyze"),
        issue_reconcile=build_issue_graph(context, export="issue-reconcile"),
        report=build_report_graph(context),
        fact_baseline=build_fact_baseline_graph(context),
        surface_baseline=build_surface_baseline_graph(context),
    )


__all__ = ["QualityGraphs", "build_quality_graphs"]
