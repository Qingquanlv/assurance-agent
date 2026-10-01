from collections.abc import Mapping
from types import MappingProxyType
from typing import cast

from graph_engine.plugin_api import TaskHandler

__all__ = [
    "handlers",
    "healing_handlers",
]


def healing_handlers() -> dict[str, TaskHandler]:
    return dict(handlers())


def handlers() -> Mapping[str, TaskHandler]:
    from assurance_healing import ops
    from assurance_healing.operations.proposal import healing_handlers

    return MappingProxyType(
        {
            **ops.router.handlers(cast(TaskHandler, ops)),
            **healing_handlers(),
        }
    )
