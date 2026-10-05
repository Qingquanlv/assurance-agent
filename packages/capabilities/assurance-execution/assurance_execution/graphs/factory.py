"""Execute and rerun, each one attempt with a committed or failed outcome."""

from __future__ import annotations

from typing import Literal


from graph_engine.boot.boot import CapabilityBuildContext
from graph_engine.flow import BoundFlow, Flow
from graph_engine.flow.sources import const

from assurance_execution.contracts.agent import ExecutionPrepareInputV1, RerunPrepareInputV1
from assurance_execution.contracts.attempts import TASK_ATTEMPT_CONTRACTS
from assurance_execution.feature import ExecutionGraphs

_EXECUTE = "execution.execute"
_RUN = "execution.run"


def _one_shot(
    context: CapabilityBuildContext,
    *,
    name: str,
    step: str,
    contract_key: str,
    execution_kind: Literal["execute", "run"],
) -> BoundFlow:
    flow = Flow(
        name,
        input=RerunPrepareInputV1 if execution_kind == "run" else ExecutionPrepareInputV1,
        outcomes=("committed", "failed"),
    )
    flow.step(
        step,
        TASK_ATTEMPT_CONTRACTS[contract_key],
        on_failure="failed",
        route_on="admission",
        routes={"committed": "committed", "failed": "failed"},
        inputs={"execution_kind": const(execution_kind)},
    )
    return flow.bind(context)


def build_execution_graphs(context: CapabilityBuildContext) -> ExecutionGraphs:
    return ExecutionGraphs(
        execute=_one_shot(
            context,
            name="execute",
            step="execute",
            contract_key="execute",
            execution_kind="execute",
        ),
        rerun=_one_shot(
            context,
            name="rerun",
            step="run",
            contract_key="run",
            execution_kind="run",
        ),
    )


__all__ = ["ExecutionGraphs", "build_execution_graphs"]
