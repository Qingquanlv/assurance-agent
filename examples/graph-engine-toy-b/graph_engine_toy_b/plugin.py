from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from graph_engine import ENGINE_API_VERSION
from graph_engine.plugin_api import (
    PluginContribution,
    PluginDescriptor,
    ProviderSource,
    RegistryPorts,
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
    (context.write_root / "left.txt").write_text("left\n", encoding="utf-8")
    return TaskOutcome.succeeded({"left": True})


async def _child(request: TaskRequest, context: TaskContext) -> TaskOutcome:
    del request
    (context.write_root / "child.txt").write_text("child\n", encoding="utf-8")
    return TaskOutcome.succeeded({"child": True})


async def _combine(request: TaskRequest, context: TaskContext) -> TaskOutcome:
    del request, context
    return TaskOutcome.succeeded({"combined": True})


@dataclass(frozen=True, slots=True)
class _Handler:
    implementation: Callable[[TaskRequest, TaskContext], Awaitable[TaskOutcome]]

    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        return await self.implementation(request, context)


class ToyBPlugin:
    @staticmethod
    def descriptor() -> PluginDescriptor:
        return PluginDescriptor(
            schema_version="1",
            source=ProviderSource(
                distribution="graph-engine-toy-b",
                version="1.0.0",
                entrypoint_group="graph_engine.plugins",
                entrypoint_name="toy-b",
                entrypoint_value="graph_engine_toy_b.plugin:ToyBPlugin",
                declaration_path="graph_engine_toy_b/plugin-declaration.json",
                import_roots=("",),
            ),
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
    def contribute(ports: RegistryPorts) -> PluginContribution:
        if ports.engine_api != ENGINE_API_VERSION:
            raise ValueError(f"unsupported engine API: {ports.engine_api!r}")
        return PluginContribution(
            task_handlers={
                "toy.b.seed": _Handler(_seed),
                "toy.b.left": _Handler(_left),
                "toy.b.child": _Handler(_child),
                "toy.b.combine": _Handler(_combine),
            },
        )
