from __future__ import annotations

from typing import Literal

from graph_engine.plugin_api import FrozenModel

RAW_AGENT_RUNTIME_BINDING_SCHEMA_VERSION = "raw-agent-runtime-binding-v1"


class AgentRuntimePolicy(FrozenModel):
    request_policy_handle: str
    request_config_handle: str


class AgentRuntimeBinding(FrozenModel):
    contract_id: str
    runtime_handler_id: str
    provider: str
    model: str
    policy: AgentRuntimePolicy
    secret_handles: tuple[str, ...]


class RawAgentRuntimeBindingProjectionV1(FrozenModel):
    schema_version: Literal["raw-agent-runtime-binding-v1"]
    contract_id: str
    contract_digest: str
    runtime_handler_id: str
    adapter: str
    provider: str
    model: str
    policy: AgentRuntimePolicy
    secret_handles: tuple[str, ...]
    activity_recovery: Literal["adopt-observe-reconcile-v1"]


class AgentRuntimeCapabilities(FrozenModel):
    pass


__all__ = [
    "AgentRuntimeBinding",
    "AgentRuntimeCapabilities",
    "AgentRuntimePolicy",
    "RAW_AGENT_RUNTIME_BINDING_SCHEMA_VERSION",
    "RawAgentRuntimeBindingProjectionV1",
]
