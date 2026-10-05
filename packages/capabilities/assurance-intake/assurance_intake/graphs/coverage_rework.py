"""One step that writes the case-rework document for the next coverage round."""

from __future__ import annotations

from graph_engine.boot.boot import CapabilityBuildContext
from graph_engine.flow import BoundFlow, Flow

from assurance_intake.contracts.coverage_rework import CoverageReworkInputV1
from assurance_intake.ops.coverage_rework import op as coverage_rework


def build_coverage_rework_graph(context: CapabilityBuildContext) -> BoundFlow:
    flow = Flow(
        "coverage-rework",
        input=CoverageReworkInputV1,
        outcomes=("done", "failed"),
    )
    flow.step("coverage-rework", coverage_rework, then="done", on_failure="failed")
    return flow.bind(context)


__all__ = ["build_coverage_rework_graph"]
