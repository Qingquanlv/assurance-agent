"""Locked runtime selection that Product binds into every Agent prepare request."""

from __future__ import annotations

from pydantic import Field

from graph_engine.plugin_api import FrozenModel

from agent_runtime_contracts.models import FrozenExecutionSelection
from agent_runtime_contracts.ops.errors import validate_model

_SHA256 = r"^[0-9a-f]{64}$"


class AgentBindingDataV1(FrozenModel):
    agent_profile: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    execution: FrozenExecutionSelection
    request_policy_digest: str = Field(pattern=_SHA256)
    request_config_digest: str = Field(pattern=_SHA256)


def validate_binding(data: object) -> AgentBindingDataV1:
    return validate_model(AgentBindingDataV1, data)
