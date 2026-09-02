from __future__ import annotations

from collections.abc import Mapping

from graph_engine import ENGINE_API_VERSION
from graph_engine.attempts.contracts import ResolvedAttemptContract, TaskAttemptContract
from graph_engine.plugin_api import (
    PluginContribution,
    PluginDescriptor,
    ProviderSource,
    RegistryPorts,
    WorkspaceProvider,
)

from graph_engine_toy_b.contracts import CONTRACT_REFS, CONTRACTS, bind_executors

_SOURCE = ProviderSource(
    distribution="graph-engine-toy-b",
    version="1.0.0",
    entrypoint_group="graph_engine.plugins",
    entrypoint_name="toy-b",
    entrypoint_value="graph_engine_toy_b.plugin:ToyBPlugin",
    declaration_path="graph_engine_toy_b/plugin-declaration.json",
    import_roots=("",),
)


class ToyBPlugin:
    @staticmethod
    def descriptor() -> PluginDescriptor:
        return PluginDescriptor(
            schema_version="1",
            source=_SOURCE,
            plugin_id="toy.b",
            plugin_version="1.0.0",
            engine_api=ENGINE_API_VERSION,
            task_handlers=(),
            commit_validators=(),
            attempt_contracts=CONTRACT_REFS,
        )

    @staticmethod
    def contribute(ports: RegistryPorts) -> PluginContribution:
        if ports.engine_api != ENGINE_API_VERSION:
            raise ValueError(f"unsupported engine API: {ports.engine_api!r}")
        return PluginContribution(attempt_contracts=CONTRACT_REFS)

    @staticmethod
    def published_attempt_contracts() -> tuple[TaskAttemptContract[object, object], ...]:
        return CONTRACTS

    @staticmethod
    def bind_attempt_executors(
        workspace: WorkspaceProvider,
    ) -> Mapping[str, ResolvedAttemptContract[object, object]]:
        return bind_executors(workspace)
