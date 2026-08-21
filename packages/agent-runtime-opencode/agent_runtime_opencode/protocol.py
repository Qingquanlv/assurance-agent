from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Literal, Self
from urllib.parse import urljoin

import httpx
from pydantic import Field, model_validator

from agent_runtime_contracts.schema import canonical_json_bytes
from agent_runtime_opencode.config import OpenCodeAdapterConfig, endpoint_origin
from graph_engine.plugin_api import FrozenModel


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


class _RedirectTarget(FrozenModel):
    location: str
    origin: str

    @model_validator(mode="after")
    def _reject_cross_origin(self) -> Self:
        if endpoint_origin(self.location) != self.origin:
            raise ValueError("redirects to a different origin")
        return self


def canonical_json_text(value: object) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def _path_segment(value: str, label: str) -> str:
    if not value or any(marker in value for marker in ("/", "\\", "?", "#", "..")):
        raise ValueError(f"{label} is not a pinned path segment")
    return value


class OpenCodeHttpClient:
    def __init__(self, config: OpenCodeAdapterConfig, *, secret: bytes) -> None:
        self._config = config
        self._origin = config.origin
        self._client = httpx.AsyncClient(
            base_url=self._origin,
            timeout=httpx.Timeout(config.request_timeout_seconds),
            follow_redirects=False,
            headers={"Authorization": f"Bearer {secret.decode('utf-8')}"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get_server_identity(self) -> dict[str, Any]:
        return await self._json("GET", "/global/health")

    async def get_profile(self) -> dict[str, Any]:
        return await self._json("GET", "/config")

    async def list_sessions(self) -> dict[str, Any] | list[Any]:
        return await self._json("GET", "/session")

    async def create_session(self, body: Mapping[str, object]) -> dict[str, Any]:
        return await self._json("POST", "/session", body)

    async def get_session(self, session_id: str) -> dict[str, Any]:
        return await self._json("GET", f"/session/{_path_segment(session_id, 'session id')}")

    async def admit_message(self, session_id: str, body: Mapping[str, object]) -> dict[str, Any]:
        path = f"/session/{_path_segment(session_id, 'session id')}/prompt_async"
        return await self._json("POST", path, body)

    async def get_message(self, session_id: str, message_id: str) -> dict[str, Any]:
        path = (
            f"/session/{_path_segment(session_id, 'session id')}"
            f"/message/{_path_segment(message_id, 'message id')}"
        )
        return await self._json("GET", path)

    async def get_status(self) -> dict[str, Any]:
        return await self._json("GET", "/session/status")

    async def open_sse(self) -> bytes:
        return await self._read("GET", "/event", accept="text/event-stream")

    async def abort(self, session_id: str) -> dict[str, Any]:
        path = f"/session/{_path_segment(session_id, 'session id')}/abort"
        return await self._json("POST", path, {})

    async def _json(
        self,
        method: str,
        path: str,
        body: Mapping[str, object] | None = None,
    ) -> Any:
        payload = json.loads(await self._read(method, path, body=body, accept="application/json"))
        if not isinstance(payload, dict | list):
            raise ValueError("response shape must be a JSON object or array")
        return payload

    async def _read(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, object] | None = None,
        accept: str,
        _redirects: int = 0,
    ) -> bytes:
        response = await self._client.request(
            method,
            path,
            headers={"Accept": accept},
            params={"directory": self._config.project_scope},
            json=None if body is None else dict(body),
        )
        if response.status_code in {301, 302, 303, 307, 308}:
            if _redirects >= 1:
                raise ValueError("redirect hop bound exceeded")
            location = response.headers.get("location")
            if not location:
                raise ValueError("redirect is missing a location")
            absolute = urljoin(str(response.url), location)
            _RedirectTarget.model_validate({"location": absolute, "origin": self._origin})
            await response.aclose()
            return await self._read(method, absolute, body=body, accept=accept, _redirects=_redirects + 1)
        response.raise_for_status()
        chunks: list[bytes] = []
        total = 0
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > self._config.max_response_bytes:
                await response.aclose()
                raise ValueError("response exceeds max_response_bytes")
            chunks.append(chunk)
        return b"".join(chunks)
