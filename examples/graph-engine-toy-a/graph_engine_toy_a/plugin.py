from __future__ import annotations

from collections.abc import Mapping

from graph_engine import ENGINE_API_VERSION
from graph_engine.attempts.models.contracts import ResolvedAttemptContract, TaskAttemptContract
from graph_engine.plugin_api import (
    AttemptContractRef,
    PluginContribution,
    PluginDescriptor,
    ProviderSource,
    RegistryPorts,
    WorkspaceProvider,
)

from graph_engine_toy_a.contracts import (
    GREET_CONTRACT,
    GREET_CONTRACT_REF,
    bind_greet_executor,
)

_SOURCE = ProviderSource(
    distribution="graph-engine-toy-a",
    version="1.0.0",
    entrypoint_group="graph_engine.plugins",
    entrypoint_name="toy-a",
    entrypoint_value="graph_engine_toy_a.plugin:ToyAPlugin",
    declaration_path="graph_engine_toy_a/plugin-declaration.json",
    import_roots=("", "graph_engine_toy_a"),
)


class ToyAPlugin:
    @staticmethod
    def descriptor() -> PluginDescriptor:
        return PluginDescriptor(
            schema_version="1",
            source=_SOURCE,
            plugin_id="toy.a",
            plugin_version="1.0.0",
            engine_api=ENGINE_API_VERSION,
            task_handlers=(),
            commit_validators=(),
            attempt_contracts=(GREET_CONTRACT_REF,),
        )

    @staticmethod
    def contribute(ports: RegistryPorts) -> PluginContribution:
        if ports.engine_api != ENGINE_API_VERSION:
            raise ValueError(f"unsupported engine API: {ports.engine_api!r}")
        return PluginContribution(attempt_contracts=(GREET_CONTRACT_REF,))

    @staticmethod
    def published_attempt_contracts() -> tuple[TaskAttemptContract[object, object], ...]:
        return (GREET_CONTRACT,)

    @staticmethod
    def published_attempt_contract_refs() -> tuple[AttemptContractRef, ...]:
        return (GREET_CONTRACT_REF,)

    @staticmethod
    def bind_attempt_executors(
        workspace: WorkspaceProvider,
        *,
        fail_first: bool = False,
    ) -> Mapping[str, ResolvedAttemptContract[object, object]]:
        resolved = bind_greet_executor(workspace, fail_first=fail_first)
        return {resolved.contract.contract_id: resolved}
