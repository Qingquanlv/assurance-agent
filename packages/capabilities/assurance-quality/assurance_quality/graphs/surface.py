"""Surface baseline. The task decides ready or not_ready from the requested families."""

from __future__ import annotations


from graph_engine.boot.boot import CapabilityBuildContext
from graph_engine.flow import BoundFlow, Flow

from assurance_quality.contracts.attempts import TASK_ATTEMPT_CONTRACTS
from assurance_quality.contracts.surface import SurfaceProbeInputV1

_SURFACE = TASK_ATTEMPT_CONTRACTS["surface-baseline"]


def build_surface_baseline_graph(context: CapabilityBuildContext) -> BoundFlow:
    flow = Flow("surface-baseline", input=SurfaceProbeInputV1, outcomes=("ready", "not_ready", "failed"))
    flow.step(
        "surface-baseline",
        _SURFACE,
        on_failure="failed",
        route_on="readiness",
        routes={"ready": "ready", "not_ready": "not_ready"},
    )
    return flow.bind(context)


__all__ = ["build_surface_baseline_graph"]
