from collections.abc import Mapping
from types import MappingProxyType

from graph_engine.plugin_api import TaskHandler

from assurance_execution.operations.normalize import NormalizeHandler
from assurance_execution.operations.runner import (
    ConfinedExecutionProcessHost,
    ExecutionProcessHost,
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
            "assurance.execution.normalize": NormalizeHandler(),
            "assurance.execution.run-tests": RunTestsHandler(process_host=host),
            "assurance.execution.select": SelectHandler(),
        }
    )


__all__ = [
    "ConfinedExecutionProcessHost",
    "ExecutionProcessHost",
    "NormalizeHandler",
    "RunTestsHandler",
    "SelectHandler",
    "execution_handlers",
]
