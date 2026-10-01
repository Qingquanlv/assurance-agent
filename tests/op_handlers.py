"""Address one declared op handler through its capability's single ``ops`` entry module."""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Any

from graph_engine.plugin_api import TaskContext, TaskHandler, TaskOutcome, TaskRequest


@dataclass(frozen=True, slots=True)
class _RoutedHandler:
    entry: Any
    handler_id: str

    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        routed = request.model_copy(update={"capability_id": self.handler_id, "target_capability_id": None})
        return await self.entry.execute(routed, context)


def op_handler(handler_id: str) -> TaskHandler:
    owner = ".".join(handler_id.split(".")[:2])
    entry = importlib.import_module(f"{owner.replace('.', '_')}.ops")
    return _RoutedHandler(entry, handler_id)


def finalize_input(prepare: dict[str, Any], agent_result: Any = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"prepare": prepare}
    if agent_result is not None:
        payload["agent_result"] = agent_result
    return payload


__all__ = ["finalize_input", "op_handler"]
