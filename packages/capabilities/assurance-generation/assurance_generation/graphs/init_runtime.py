"""Initialize the test runtime. One attempt, then completed or failed."""

from __future__ import annotations


from graph_engine.boot.boot import CapabilityBuildContext
from graph_engine.flow import BoundFlow, Flow

from assurance_generation.contracts.attempts import TASK_ATTEMPT_CONTRACTS
from assurance_generation.contracts.init_runtime import InitTestRuntimeInputV1


def build_init_runtime_graph(context: CapabilityBuildContext) -> BoundFlow:
    flow = Flow("init-runtime", input=InitTestRuntimeInputV1, outcomes=("completed", "failed"))
    flow.step(
        "init-test-runtime",
        TASK_ATTEMPT_CONTRACTS["init-test-runtime"],
        on_failure="failed",
        then="completed",
    )
    return flow.bind(context)


__all__ = ["build_init_runtime_graph"]
