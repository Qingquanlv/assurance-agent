"""Host-only transport for prepared business data and the Agent request."""

from __future__ import annotations

from graph_engine.plugin_api import FrozenModel

from agent_runtime_contracts.wire.models import AgentRunRequest, JSONValue


class PreparedAgentRun(FrozenModel):
    """Only run_request is dispatched to the external Agent."""

    run_request: AgentRunRequest
    prepared_business: dict[str, JSONValue]


__all__ = ["PreparedAgentRun"]
