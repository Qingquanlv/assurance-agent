from __future__ import annotations

from graph_engine.boot.boot import CapabilityBuildContext

from assurance_intake.feature import IntakeGraphs
from assurance_intake.graphs.case import build_case_graph
from assurance_intake.graphs.coverage_rework import build_coverage_rework_graph
from assurance_intake.graphs.prepare import build_prepare_graph


def build_intake_graphs(context: CapabilityBuildContext) -> IntakeGraphs:
    return IntakeGraphs(
        prepare=build_prepare_graph(context),
        case=build_case_graph(context),
        coverage_rework=build_coverage_rework_graph(context),
    )


__all__ = ["IntakeGraphs", "build_intake_graphs"]
