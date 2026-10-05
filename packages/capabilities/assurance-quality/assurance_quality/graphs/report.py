"""Report. Normal publication is reported; a diagnostic publication stays diagnostic."""

from __future__ import annotations

from graph_engine.boot.boot import CapabilityBuildContext
from graph_engine.flow import BoundFlow, Flow

from assurance_quality.contracts.assessment import ReportBoundInputV1
from assurance_quality.ops.report import op as report


def build_report_graph(context: CapabilityBuildContext) -> BoundFlow:
    flow = Flow("report", input=ReportBoundInputV1, outcomes=("reported", "diagnostic", "failed"))
    flow.step(
        "report",
        report,
        on_failure="failed",
        route_on="publication",
        routes={"reported": "reported", "diagnostic": "diagnostic", "failed": "failed"},
    )
    return flow.bind(context)


__all__ = ["build_report_graph"]
