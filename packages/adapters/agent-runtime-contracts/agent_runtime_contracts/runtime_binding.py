from __future__ import annotations

from graph_engine.plugin_api import FrozenModel


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


class AgentRuntimeCapabilities(FrozenModel):
    pass


__all__ = [
    "AgentRuntimeBinding",
    "AgentRuntimeCapabilities",
    "AgentRuntimePolicy",
]
