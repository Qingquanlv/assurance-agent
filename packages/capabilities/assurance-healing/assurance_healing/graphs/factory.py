from __future__ import annotations

from graph_engine.boot.boot import CapabilityBuildContext

from assurance_healing.feature import HealingGraphs
from assurance_healing.graphs.coverage import build_repair_coverage_graph
from assurance_healing.graphs.failure import build_repair_failure_graph


def build_healing_graphs(context: CapabilityBuildContext) -> HealingGraphs:
    return HealingGraphs(
        repair_failure=build_repair_failure_graph(context),
        repair_coverage=build_repair_coverage_graph(context),
    )


__all__ = ["HealingGraphs", "build_healing_graphs"]
