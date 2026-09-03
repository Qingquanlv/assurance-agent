from __future__ import annotations

import json
from collections.abc import Mapping
from typing import cast

from graph_engine import ENGINE_API_VERSION
from graph_engine.attempts.contracts import ResolvedAttemptContract, TaskAttemptContract
from graph_engine.canonical import JSONValue
from graph_engine.plugin_api import (
    CapabilityBindingContribution,
    PluginContribution,
    PluginDependency,
    PluginDescriptor,
    ProviderSource,
    RegistryPorts,
    WorkspaceProvider,
)

from agent_runtime_fixture import RESULT_SCHEMA_RESOURCE_ID, RUN_CAPABILITY_ID, package_resource_bytes
from agent_runtime_fixture.contracts import RUN_CONTRACT, RUN_CONTRACT_REF, bind_run_executor

_OPENCODE_SOURCE = ProviderSource(
    distribution="agent-runtime-fixture",
    version="1.0.0",
    entrypoint_group="graph_engine.plugins",
    entrypoint_name="opencode-binding",
    entrypoint_value="agent_runtime_fixture.bindings:OpenCodeBindingPlugin",
    declaration_path="agent_runtime_fixture/opencode-binding-declaration.json",
    import_roots=("", "agent_runtime_fixture"),
)
_CURSOR_SOURCE = ProviderSource(
    distribution="agent-runtime-fixture",
    version="1.0.0",
    entrypoint_group="graph_engine.plugins",
    entrypoint_name="cursor-binding",
    entrypoint_value="agent_runtime_fixture.bindings:CursorBindingPlugin",
    declaration_path="agent_runtime_fixture/cursor-binding-declaration.json",
    import_roots=("", "agent_runtime_fixture"),
)


def _result_schema() -> dict[str, JSONValue]:
    parsed = json.loads(package_resource_bytes()[RESULT_SCHEMA_RESOURCE_ID].decode("utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError("fixture result schema must be a JSON object")
    return cast(dict[str, JSONValue], parsed)


def _descriptor(source: ProviderSource, adapter_plugin_id: str) -> PluginDescriptor:
    return PluginDescriptor(
        schema_version="1",
        source=source,
        plugin_id="fixture.binding",
        plugin_version="1.0.0",
        engine_api=ENGINE_API_VERSION,
        task_handlers=(),
        commit_validators=(),
        dependencies=(
            PluginDependency(plugin_id="fixture.runtime", version_specifier="==1.0.0"),
            PluginDependency(plugin_id=adapter_plugin_id, version_specifier="==0.1.0"),
        ),
        bindings=(RUN_CAPABILITY_ID,),
    )


def _contribute(target: str, adapter: dict[str, JSONValue]) -> PluginContribution:
    return PluginContribution(
        bindings=(
            CapabilityBindingContribution(
                capability_id=RUN_CAPABILITY_ID,
                target_capability_id=target,
                data={
                    "adapter": adapter,
                    "result_schema": _result_schema(),
                },
            ),
        ),
    )


def published_attempt_contracts() -> tuple[TaskAttemptContract[object, object], ...]:
    return (RUN_CONTRACT,)


def bind_attempt_executors(
    workspace: WorkspaceProvider,
) -> Mapping[str, ResolvedAttemptContract[object, object]]:
    resolved = bind_run_executor(workspace)
    return {resolved.contract.contract_id: resolved}


class OpenCodeBindingPlugin:
    @staticmethod
    def descriptor() -> PluginDescriptor:
        return _descriptor(_OPENCODE_SOURCE, "runtime.opencode")

    @staticmethod
    def contribute(ports: RegistryPorts) -> PluginContribution:
        if ports.engine_api != ENGINE_API_VERSION:
            raise ValueError(f"unsupported engine API: {ports.engine_api!r}")
        return _contribute(
            "runtime.opencode.execute",
            {
                "distribution": "agent-runtime-opencode",
                "entrypoint_name": "opencode",
                "protocol_profile": "opencode-http-v1",
            },
        )

    published_attempt_contracts = staticmethod(published_attempt_contracts)
    bind_attempt_executors = staticmethod(bind_attempt_executors)


class CursorBindingPlugin:
    @staticmethod
    def descriptor() -> PluginDescriptor:
        return _descriptor(_CURSOR_SOURCE, "runtime.cursor")

    @staticmethod
    def contribute(ports: RegistryPorts) -> PluginContribution:
        if ports.engine_api != ENGINE_API_VERSION:
            raise ValueError(f"unsupported engine API: {ports.engine_api!r}")
        return _contribute(
            "runtime.cursor.execute",
            {
                "distribution": "agent-runtime-cursor",
                "entrypoint_name": "cursor",
                "protocol_profile": "confined_process",
            },
        )

    published_attempt_contracts = staticmethod(published_attempt_contracts)
    bind_attempt_executors = staticmethod(bind_attempt_executors)
