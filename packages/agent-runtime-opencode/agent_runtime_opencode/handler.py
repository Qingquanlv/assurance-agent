from __future__ import annotations

from typing import Any

from graph_engine.plugin_api import SecretHandleUnauthorized, TaskContext, TaskOutcome, TaskRequest

from agent_runtime_opencode.config import OpenCodeAdapterConfig
from agent_runtime_opencode.protocol import (
    AcceptedOpenCodeProfile,
    OpenCodeHttpClient,
    canonical_json_text,
)
from agent_runtime_contracts.schema import canonical_digest


class OpenCodeHandler:
    def __init__(self, config: OpenCodeAdapterConfig | None = None) -> None:
        self._config = config

    async def preflight(self, request: TaskRequest, context: TaskContext) -> dict[str, Any]:
        del request
        if self._config is None:
            raise ValueError("OpenCode adapter config is required")
        if context.secrets is None:
            raise SecretHandleUnauthorized("secret port is required")
        config = self._config
        secret = context.secrets.resolve(config.secret_handle)
        client = OpenCodeHttpClient(config, secret=secret)
        try:
            identity = await client.get_server_identity()
            advertised = await client.get_profile()
            profile = AcceptedOpenCodeProfile.model_validate(advertised)
            fingerprint = {
                "endpoint_origin": config.origin,
                "tls_identity_digest": config.tls_identity_digest,
                "protocol_profile": profile.protocol_profile,
                "prompt_admission": profile.prompt_admission,
                "prompt_idempotency": profile.prompt_idempotency,
                "server_identity_digest": canonical_digest(identity),
                "project_scope": config.project_scope,
            }
            text = canonical_json_text(fingerprint)
            secret_text = secret.decode("utf-8")
            if "secret" in text or secret_text in text:
                raise ValueError("credentials must not enter preflight fingerprint")
            return fingerprint
        finally:
            await client.aclose()

    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del request, context
        raise NotImplementedError("OpenCode execute requires discovery")
