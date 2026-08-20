from __future__ import annotations

from typing import cast

from graph_engine import ENGINE_API_VERSION
from graph_engine.canonical import JSONValue
from graph_engine.plugin_api import (
    EnginePorts,
    PluginDescriptor,
    PluginRuntime,
    TaskContext,
    TaskOutcome,
    TaskRequest,
)


async def _greet(request: TaskRequest, context: TaskContext) -> TaskOutcome:
    config = cast(dict[str, str], cast(dict[str, JSONValue], request.input)["config"])
    name = config["name"]
    message = f"hello {name}"
    (context.workspace_root / "greeting.txt").write_text(f"{message}\n", encoding="utf-8")
    return TaskOutcome.succeeded(cast(JSONValue, {"message": message}))


class ToyAPlugin:
    @staticmethod
    def descriptor() -> PluginDescriptor:
        return PluginDescriptor(
            plugin_id="toy.a",
            plugin_version="1.0.0",
            engine_api=ENGINE_API_VERSION,
            task_handlers=("toy.a.greet",),
            commit_validators=(),
        )

    @staticmethod
    def bind(ports: EnginePorts) -> PluginRuntime:
        if ports.engine_api != ENGINE_API_VERSION:
            raise ValueError(f"unsupported engine API: {ports.engine_api!r}")
        return PluginRuntime(task_handlers={"toy.a.greet": _greet}, commit_validators={})
