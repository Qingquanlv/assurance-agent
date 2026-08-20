from __future__ import annotations

from typing import cast

from graph_engine import ENGINE_API_VERSION
from graph_engine.canonical import JSONValue
from graph_engine.plugin_api import (
    PluginContribution,
    PluginDescriptor,
    ProviderSource,
    RegistryPorts,
    TaskContext,
    TaskOutcome,
    TaskRequest,
)


class _GreetHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        config = cast(dict[str, str], cast(dict[str, JSONValue], request.input)["config"])
        name = config["name"]
        message = f"hello {name}"
        (context.workspace_root / "greeting.txt").write_text(f"{message}\n", encoding="utf-8")
        return TaskOutcome.succeeded(cast(JSONValue, {"message": message}))


class ToyAPlugin:
    @staticmethod
    def descriptor() -> PluginDescriptor:
        return PluginDescriptor(
            schema_version="1",
            source=ProviderSource(
                distribution="graph-engine-toy-a",
                version="1.0.0",
                entrypoint_group="graph_engine.plugins",
                entrypoint_name="toy-a",
                entrypoint_value="graph_engine_toy_a.plugin:ToyAPlugin",
                declaration_path="graph_engine_toy_a/plugin-declaration.json",
                import_roots=("",),
            ),
            plugin_id="toy.a",
            plugin_version="1.0.0",
            engine_api=ENGINE_API_VERSION,
            task_handlers=("toy.a.greet",),
            commit_validators=(),
        )

    @staticmethod
    def contribute(ports: RegistryPorts) -> PluginContribution:
        if ports.engine_api != ENGINE_API_VERSION:
            raise ValueError(f"unsupported engine API: {ports.engine_api!r}")
        return PluginContribution(task_handlers={"toy.a.greet": _GreetHandler()})
