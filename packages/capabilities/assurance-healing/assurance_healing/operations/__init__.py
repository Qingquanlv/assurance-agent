from collections.abc import Mapping
from types import MappingProxyType
from typing import cast

from graph_engine.plugin_api import TaskHandler

__all__ = ["handlers"]


def handlers() -> Mapping[str, TaskHandler]:
    from assurance_healing import ops

    return MappingProxyType(dict(ops.router.handlers(cast(TaskHandler, ops))))
