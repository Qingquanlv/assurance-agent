from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from graph_engine.plugin_api import TaskHandler

from assurance_healing.operations.proposal import healing_handlers

__all__ = ["healing_handlers"]


def handlers() -> Mapping[str, TaskHandler]:
    return MappingProxyType(healing_handlers())
