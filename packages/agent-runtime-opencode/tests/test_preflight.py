from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from graph_engine.plugin_api import (
    InvocationMetadata,
    SecretHandleUnauthorized,
    SecretPort,
    TaskContext,
    TaskRequest,
)
from pydantic import ValidationError

from agent_runtime_opencode.config import OpenCodeAdapterConfig
from agent_runtime_opencode.handler import OpenCodeHandler
from agent_runtime_opencode.protocol import OpenCodeProtocolProfile, canonical_json_text
from fake_server import OpenCodeFakeServer  # pyright: ignore[reportMissingImports]


_SHA = "a" * 64
_CANARY = b"canary-secret-value"
_SECRET_TEXT = "canary-secret-value"


def _profile(**overrides: object) -> OpenCodeProtocolProfile:
    payload: dict[str, object] = {
        "prompt_idempotency": "conflict-on-body-drift",
        "metadata_supported": True,
        "sse_supported": True,
        "poll_fallback_supported": True,
    }
    payload.update(overrides)
    return OpenCodeProtocolProfile.model_validate(payload)


def _config(fake: OpenCodeFakeServer, **overrides: object) -> OpenCodeAdapterConfig:
    payload: dict[str, object] = {
        "schema_version": "1",
        "endpoint": fake.base_url,
        "tls_identity_digest": _SHA,
        "secret_handle": "opencode.token",
        "protocol_profile": "opencode-http-v1",
        "project_scope": fake.project_scope,
        "request_timeout_seconds": 5,
        "observation_horizon_seconds": 30,
        "max_response_bytes": 65536,
    }
    payload.update(overrides)
    return OpenCodeAdapterConfig.model_validate(payload)


def _request() -> TaskRequest:
    return TaskRequest(
        invocation_id="inv-1",
        task_id="task-1",
        graph_instance_id="graph-1",
        node_id="run",
        capability_id="runtime.opencode.execute",
        invocation=InvocationMetadata(
            invocation_id="inv-1",
            lock_digest=_SHA,
            composition_digest="b" * 64,
            entrypoint="runtime.opencode.execute",
        ),
        attempt=1,
        input={"schema_version": "1"},
    )


class _ExactSecretPort:
    def __init__(self, authorized: dict[str, bytes]) -> None:
        self._authorized = dict(authorized)

    def resolve(self, handle: str) -> bytes:
        try:
            return bytes(self._authorized[handle])
        except KeyError as error:
            raise SecretHandleUnauthorized(f"unauthorized secret handle: {handle}") from error


def _context(*, secrets: SecretPort | None = None, tmp_path: Path | None = None) -> TaskContext:
    return TaskContext(
        workspace_root=tmp_path or Path(".").resolve(),
        heartbeat=lambda: None,
        cancel_requested=lambda: False,
        invocation=InvocationMetadata(
            invocation_id="inv-1",
            lock_digest=_SHA,
            composition_digest="b" * 64,
            entrypoint="runtime.opencode.execute",
        ),
        secrets=secrets if secrets is not None else _ExactSecretPort({"opencode.token": _CANARY}),
    )


async def test_preflight_authenticates_idempotent_prompt_profile() -> None:
    fake = OpenCodeFakeServer(profile=_profile(prompt_idempotency="conflict-on-body-drift"))
    try:
        fingerprint = await OpenCodeHandler(_config(fake)).preflight(_request(), _context())
        assert fingerprint["prompt_admission"] == "caller-message-id-v1"
        assert "secret" not in canonical_json_text(fingerprint)
        assert _SECRET_TEXT not in canonical_json_text(fingerprint)
        assert fake.count("GET", "/global/health") == 1
        assert fake.count("GET", "/config") == 1
        recorded = fake.records[0]
        assert recorded.method == "GET"
        assert recorded.path == "/global/health"
        assert recorded.body_digest == hashlib.sha256(b"").hexdigest()
    finally:
        fake.close()


async def test_preflight_rejects_undeclared_secret_handle() -> None:
    fake = OpenCodeFakeServer(profile=_profile())
    try:
        with pytest.raises(SecretHandleUnauthorized):
            await OpenCodeHandler(_config(fake)).preflight(
                _request(),
                _context(secrets=_ExactSecretPort({})),
            )
        assert fake.records == ()
    finally:
        fake.close()


async def test_preflight_rejects_cross_origin_redirect() -> None:
    fake = OpenCodeFakeServer(
        profile=_profile(),
        redirect_location="http://example.com/steal",
    )
    try:
        with pytest.raises(ValidationError, match="origin"):
            await OpenCodeHandler(_config(fake)).preflight(_request(), _context())
    finally:
        fake.close()


async def test_preflight_rejects_unsupported_prompt_admission() -> None:
    fake = OpenCodeFakeServer(profile=_profile(prompt_idempotency="accepts-message-id-only"))
    try:
        with pytest.raises(ValidationError, match="prompt"):
            await OpenCodeHandler(_config(fake)).preflight(_request(), _context())
    finally:
        fake.close()


@pytest.mark.parametrize(
    "cut",
    [
        "before_request",
        "after_provider_mutation",
        "before_response",
        "malformed_response",
        "sse_gap",
        "polling_lag",
    ],
)
def test_fake_server_accepts_documented_cuts(cut: str) -> None:
    fake = OpenCodeFakeServer(profile=_profile(), cut=cut)
    try:
        assert fake.cut == cut
        assert fake.supported_cuts() == (
            "before_request",
            "after_provider_mutation",
            "before_response",
            "malformed_response",
            "sse_gap",
            "polling_lag",
        )
    finally:
        fake.close()
