from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import Field

from agent_runtime_contracts.runtime_binding import AgentRuntimeCapabilities
from graph_engine.plugin_api import FrozenModel

from agent_runtime_opencode.config import OpenCodeAdapterConfig


OPENCODE_RUNTIME_CAPABILITIES = AgentRuntimeCapabilities()


class OpenCodeProtocolProfile(FrozenModel):
    schema_version: Literal["1"] = "1"
    protocol_profile: Literal["opencode-http-v1"] = "opencode-http-v1"
    prompt_admission: Literal["caller-message-id-v1"] = "caller-message-id-v1"
    prompt_idempotency: str = Field(min_length=1)
    metadata_supported: bool = True
    sse_supported: bool = True
    poll_fallback_supported: bool = True


class AcceptedOpenCodeProfile(FrozenModel):
    protocol_profile: Literal["opencode-http-v1"]
    prompt_admission: Literal["caller-message-id-v1"]
    prompt_idempotency: Literal["conflict-on-body-drift"]
    metadata_supported: Literal[True]
    sse_supported: Literal[True]
    poll_fallback_supported: bool = True


_LOCKED_PROMPT_IDEMPOTENCY = "conflict-on-body-drift"


def locked_opencode_profile(config: OpenCodeAdapterConfig) -> AcceptedOpenCodeProfile:
    defaults = OpenCodeProtocolProfile.model_validate({"prompt_idempotency": _LOCKED_PROMPT_IDEMPOTENCY})
    return AcceptedOpenCodeProfile(
        protocol_profile=config.protocol_profile,
        prompt_admission=defaults.prompt_admission,
        prompt_idempotency=_LOCKED_PROMPT_IDEMPOTENCY,
        metadata_supported=True,
        sse_supported=True,
        poll_fallback_supported=defaults.poll_fallback_supported,
    )


def resolve_advertised_profile(
    advertised: Mapping[str, Any],
    config: OpenCodeAdapterConfig,
) -> AcceptedOpenCodeProfile:
    if "protocol_profile" not in advertised:
        return locked_opencode_profile(config)
    profile = AcceptedOpenCodeProfile.model_validate(advertised)
    if profile.protocol_profile != config.protocol_profile:
        raise ValueError(
            f"advertised protocol profile {profile.protocol_profile!r} "
            f"does not match locked binding {config.protocol_profile!r}"
        )
    return profile


def advertised_runtime_capabilities() -> AgentRuntimeCapabilities:
    return OPENCODE_RUNTIME_CAPABILITIES
