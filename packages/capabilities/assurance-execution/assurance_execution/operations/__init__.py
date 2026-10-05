from collections.abc import Mapping
from types import MappingProxyType

from graph_engine.plugin_api import TaskHandler

from assurance_execution.operations.runner import (
    ConfinedExecutionProcessHost,
    ExecutionProcessHost,
    RunTestsHandler,
)


def execution_handlers(
    *,
    process_host: ExecutionProcessHost | None = None,
) -> Mapping[str, TaskHandler]:
    host = process_host or ConfinedExecutionProcessHost()
    return MappingProxyType(
        {
            "assurance.execution.run-tests": RunTestsHandler(process_host=host),
        }
    )


__all__ = [
    "ConfinedExecutionProcessHost",
    "ExecutionProcessHost",
    "RunTestsHandler",
    "execution_handlers",
]
