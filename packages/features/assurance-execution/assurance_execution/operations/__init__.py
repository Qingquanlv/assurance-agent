from collections.abc import Mapping
from types import MappingProxyType

from graph_engine.plugin_api import TaskHandler

from assurance_execution.operations.agent_skills import (
    ExecuteFinalizeHandler,
    ExecutePrepareHandler,
    RunFinalizeHandler,
    RunPrepareHandler,
)
from assurance_execution.operations.normalize import NormalizeHandler
from assurance_execution.operations.runner import (
    ConfinedExecutionProcessHost,
    ExecutionProcessHost,
    RunTestsAndCollectPrMetricsHandler,
    RunTestsHandler,
)
from assurance_execution.operations.selection import SelectHandler


def execution_handlers(
    *,
    process_host: ExecutionProcessHost | None = None,
) -> Mapping[str, TaskHandler]:
    host = process_host or ConfinedExecutionProcessHost()
    return MappingProxyType(
        {
            "assurance.execution.execute.finalize": ExecuteFinalizeHandler(),
            "assurance.execution.execute.prepare": ExecutePrepareHandler(),
            "assurance.execution.normalize": NormalizeHandler(),
            "assurance.execution.run-tests": RunTestsHandler(process_host=host),
            "assurance.execution.run-tests-and-collect-pr-metrics": RunTestsAndCollectPrMetricsHandler(
                process_host=host
            ),
            "assurance.execution.run.finalize": RunFinalizeHandler(),
            "assurance.execution.run.prepare": RunPrepareHandler(),
            "assurance.execution.select": SelectHandler(),
        }
    )


__all__ = [
    "ConfinedExecutionProcessHost",
    "ExecuteFinalizeHandler",
    "ExecutePrepareHandler",
    "ExecutionProcessHost",
    "NormalizeHandler",
    "RunFinalizeHandler",
    "RunPrepareHandler",
    "RunTestsAndCollectPrMetricsHandler",
    "RunTestsHandler",
    "SelectHandler",
    "execution_handlers",
]
