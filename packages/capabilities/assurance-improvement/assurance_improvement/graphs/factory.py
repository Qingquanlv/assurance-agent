from __future__ import annotations

from graph_engine.boot.boot import CapabilityBuildContext

from assurance_improvement.task import ImprovementGraphs
from assurance_improvement.graphs.delivery import (
    build_apply_graph,
    build_archive_graph,
    build_evaluate_graph,
    build_export_graph,
    build_review_graph,
    build_rollback_graph,
)
from assurance_improvement.graphs.retro import build_retro_graph


def build_improvement_graphs(context: CapabilityBuildContext) -> ImprovementGraphs:
    return ImprovementGraphs(
        archive=build_archive_graph(context),
        retro=build_retro_graph(context),
        review=build_review_graph(context),
        evaluate=build_evaluate_graph(context),
        export=build_export_graph(context),
        apply=build_apply_graph(context),
        rollback=build_rollback_graph(context),
    )


__all__ = ["ImprovementGraphs", "build_improvement_graphs"]
