from __future__ import annotations

from graph_engine import ENGINE_API_VERSION
from graph_engine.plugin_api import (
    EnginePorts,
    PluginDescriptor,
    PluginRuntime,
    TaskContext,
    TaskOutcome,
    TaskRequest,
)


async def _seed(request: TaskRequest, context: TaskContext) -> TaskOutcome:
    del request, context
    return TaskOutcome.succeeded({"route": "both"})


async def _left(request: TaskRequest, context: TaskContext) -> TaskOutcome:
    if request.attempt == 1:
        return TaskOutcome.failed("transient", "retry the left branch")
    (context.workspace_root / "left.txt").write_text("left\n", encoding="utf-8")
    return TaskOutcome.succeeded({"left": True})


async def _child(request: TaskRequest, context: TaskContext) -> TaskOutcome:
    del request
    (context.workspace_root / "child.txt").write_text("child\n", encoding="utf-8")
    return TaskOutcome.succeeded({"child": True})


async def _combine(request: TaskRequest, context: TaskContext) -> TaskOutcome:
    del request, context
    return TaskOutcome.succeeded({"combined": True})


class ToyBPlugin:
    @staticmethod
    def descriptor() -> PluginDescriptor:
        return PluginDescriptor(
            plugin_id="toy.b",
            plugin_version="1.0.0",
            engine_api=ENGINE_API_VERSION,
            task_handlers=(
                "toy.b.seed",
                "toy.b.left",
                "toy.b.child",
                "toy.b.combine",
            ),
            commit_validators=(),
        )

    @staticmethod
    def bind(ports: EnginePorts) -> PluginRuntime:
        if ports.engine_api != ENGINE_API_VERSION:
            raise ValueError(f"unsupported engine API: {ports.engine_api!r}")
        return PluginRuntime(
            task_handlers={
                "toy.b.seed": _seed,
                "toy.b.left": _left,
                "toy.b.child": _child,
                "toy.b.combine": _combine,
            },
            commit_validators={},
        )
