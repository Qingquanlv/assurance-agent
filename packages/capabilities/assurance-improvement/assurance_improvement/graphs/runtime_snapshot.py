"""One step that commits this invocation's organized runtime evidence."""

from __future__ import annotations

from graph_engine.boot.boot import CapabilityBuildContext
from graph_engine.flow import BoundFlow, Flow

from assurance_improvement.contracts.attempts import TASK_ATTEMPT_CONTRACTS
from assurance_improvement.contracts.runtime_snapshot import RetroRuntimeSnapshotInputV1

_SNAPSHOT = TASK_ATTEMPT_CONTRACTS["assurance.improvement.retro-runtime-snapshot"]


def build_runtime_snapshot_graph(context: CapabilityBuildContext) -> BoundFlow:
    flow = Flow(
        "runtime-snapshot",
        input=RetroRuntimeSnapshotInputV1,
        outcomes=("done", "failed"),
    )
    flow.step("retro-runtime-snapshot", _SNAPSHOT, then="done", on_failure="failed")
    return flow.bind(context)


__all__ = ["build_runtime_snapshot_graph"]
