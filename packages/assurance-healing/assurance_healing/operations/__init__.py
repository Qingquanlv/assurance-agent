from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from graph_engine.plugin_api import TaskHandler

__all__ = ["healing_handlers", "handlers"]


def healing_handlers() -> dict[str, TaskHandler]:
    from assurance_healing.operations.proposal import healing_handlers as _healing_handlers

    return _healing_handlers()


def handlers() -> Mapping[str, TaskHandler]:
    return MappingProxyType(healing_handlers())
