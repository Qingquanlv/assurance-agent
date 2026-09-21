from __future__ import annotations

import pytest

from agent_runtime_opencode.config import OpenCodeAdapterConfig
from agent_runtime_opencode.handler import OpenCodeHandler
from agent_runtime_opencode.protocol import (
    OpenCodeHttpClient,
    locked_opencode_profile,
    resolve_advertised_profile,
)
from fake_server import OpenCodeFakeServer  # pyright: ignore[reportMissingImports]
from test_preflight import (  # pyright: ignore[reportMissingImports]  # noqa: PLC2701
    _SHA,
    _config,
    _context,
    _profile,
    _task_request,
)


def _valid_config(**overrides: object) -> OpenCodeAdapterConfig:
    payload: dict[str, object] = {
        "schema_version": "1",
        "endpoint": "http://127.0.0.1:4096",
        "tls_identity_digest": _SHA,
        "secret_handle": "opencode.token",
        "protocol_profile": "opencode-http-v1",
        "project_scope": "/tmp/attempt-workspace",
        "request_timeout_seconds": 5,
        "observation_horizon_seconds": 30,
        "poll_interval_seconds": 0.5,
        "cancel_timeout_seconds": 5,
        "max_response_bytes": 65536,
        "adapter_configuration_digest": _SHA,
    }
    payload.update(overrides)
    return OpenCodeAdapterConfig.model_validate(payload)


def test_locked_profile_uses_binding_protocol_fields() -> None:
    config = _valid_config()
    profile = locked_opencode_profile(config)
    assert profile.protocol_profile == "opencode-http-v1"
    assert profile.prompt_admission == "caller-message-id-v1"
    assert profile.prompt_idempotency == "conflict-on-body-drift"
    assert profile.metadata_supported is True
    assert profile.sse_supported is True


def test_real_shaped_config_without_protocol_profile_uses_locked_binding() -> None:
    config = _valid_config()
    advertised = {
        "model": "provider_default",
        "mcp": {},
        "tools": [],
    }
    profile = resolve_advertised_profile(advertised, config)
    assert profile == locked_opencode_profile(config)


async def test_preflight_with_real_shaped_config_produces_fingerprint() -> None:
    fake = OpenCodeFakeServer(profile=_profile())

    class _RealConfigServer(OpenCodeFakeServer):
        def _payload(self, path: str) -> object:
            if path == "/config":
                return {"model": "provider_default", "mcp": {}, "tools": []}
            return super()._payload(path)

    server = _RealConfigServer(profile=_profile())
    try:
        fingerprint = await OpenCodeHandler().preflight(_task_request(server), _context())
        assert fingerprint["protocol_profile"] == "opencode-http-v1"
        assert fingerprint["prompt_admission"] == "caller-message-id-v1"
        assert fingerprint["prompt_idempotency"] == "conflict-on-body-drift"
        assert server.count("GET", "/config") == 1
    finally:
        server.close()
        fake.close()


async def test_fake_config_with_protocol_profile_still_validates() -> None:
    fake = OpenCodeFakeServer(profile=_profile())
    try:
        fingerprint = await OpenCodeHandler().preflight(_task_request(fake), _context())
        assert fingerprint["protocol_profile"] == "opencode-http-v1"
        assert fake.count("GET", "/config") == 1
    finally:
        fake.close()


async def test_http_client_uses_basic_opencode_password() -> None:
    import base64

    fake = OpenCodeFakeServer(profile=_profile())
    try:
        config = _config(fake)
        client = OpenCodeHttpClient(config, secret=b"tok")
        try:
            prepared = client._client.build_request("GET", "/global/health")
            header = prepared.headers["Authorization"]
            assert header.startswith("Basic ")
            assert base64.b64decode(header.split(" ", 1)[1]).decode() == "opencode:tok"
        finally:
            await client.aclose()
    finally:
        fake.close()


async def test_http_client_omits_authorization_when_secret_is_empty() -> None:
    fake = OpenCodeFakeServer(profile=_profile())
    try:
        config = _config(fake)
        client = OpenCodeHttpClient(config, secret=b"")
        try:
            prepared = client._client.build_request("GET", "/global/health")
            assert "Authorization" not in prepared.headers
            await client.get_server_identity()
        finally:
            await client.aclose()
        assert fake.count("GET", "/global/health") == 1
    finally:
        fake.close()


def test_advertised_profile_rejects_invalid_protocol_profile_shape() -> None:
    config = _valid_config()
    with pytest.raises(Exception):
        resolve_advertised_profile(
            {
                "protocol_profile": "other-profile",
                "prompt_admission": "caller-message-id-v1",
                "prompt_idempotency": "conflict-on-body-drift",
                "metadata_supported": True,
                "sse_supported": True,
            },
            config,
        )
