from __future__ import annotations

import asyncio
import base64
import json
import time
from collections.abc import Mapping
from typing import Any, Literal, Self
from urllib.parse import urljoin

import httpx
from pydantic import Field, JsonValue, model_validator

from agent_runtime_contracts.wire.schema import canonical_json_bytes
from graph_engine.plugin_api import FrozenModel

from agent_runtime_opencode.config import OpenCodeAdapterConfig, endpoint_origin


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


class OpenCodeDiscoveryMetadata(FrozenModel):
    schema_version: Literal["1"] = "1"
    invocation_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    activation_id: str = Field(min_length=1)
    attempt: int = Field(ge=1)
    activity_id: str = Field(min_length=1)
    request_digest: str = Field(min_length=64, max_length=64)
    workspace_identity_digest: str = Field(min_length=64, max_length=64)
    adapter_source_digest: str = Field(min_length=64, max_length=64)


class OpenCodeSessionMetadata(FrozenModel):
    discovery: OpenCodeDiscoveryMetadata
    workspace_binding: dict[str, JsonValue]


class OpenCodeSessionCreateRequest(FrozenModel):
    title: str = Field(min_length=1)
    metadata: OpenCodeSessionMetadata
    agent: str | None = None
    parentID: str | None = None


def _path_segment(value: str, label: str) -> str:
    if not value or any(marker in value for marker in ("/", "\\", "?", "#", "..")):
        raise ValueError(f"{label} is not a pinned path segment")
    return value


class OpenCodeHttpClient:
    def __init__(
        self,
        config: OpenCodeAdapterConfig,
        *,
        secret: bytes,
        directory: str | None = None,
    ) -> None:
        self._config = config
        self._origin = config.origin
        self._directory = directory if directory is not None else config.project_scope
        headers: dict[str, str] = {}
        if secret:
            token = secret.decode("utf-8")
            encoded = base64.b64encode(f"opencode:{token}".encode("utf-8")).decode("ascii")
            headers["Authorization"] = f"Basic {encoded}"
        self._client = httpx.AsyncClient(
            base_url=self._origin,
            timeout=httpx.Timeout(config.request_timeout_seconds),
            follow_redirects=False,
            trust_env=False,
            headers=headers,
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
        typed = OpenCodeSessionCreateRequest.model_validate(body)
        payload = typed.model_dump(mode="json", exclude_none=True)
        if "id" in payload:
            raise ValueError("create must not supply a session id")
        return await self._json("POST", "/session", payload)

    async def get_session(self, session_id: str) -> dict[str, Any]:
        return await self._json("GET", f"/session/{_path_segment(session_id, 'session id')}")

    async def update_session(self, session_id: str, body: Mapping[str, object]) -> dict[str, Any]:
        return await self._json("PATCH", f"/session/{_path_segment(session_id, 'session id')}", body)

    async def admit_message(self, session_id: str, body: Mapping[str, object]) -> dict[str, Any]:
        path = f"/session/{_path_segment(session_id, 'session id')}/prompt_async"
        raw = await self._read("POST", path, body=body, accept="application/json")
        if not raw.strip():
            return {}
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("admission response must be a JSON object")
        return payload

    async def get_message(self, session_id: str, message_id: str) -> dict[str, Any]:
        path = (
            f"/session/{_path_segment(session_id, 'session id')}"
            f"/message/{_path_segment(message_id, 'message id')}"
        )
        return await self._json("GET", path)

    async def list_messages(self, session_id: str) -> list[Any]:
        path = f"/session/{_path_segment(session_id, 'session id')}/message"
        payload = await self._json("GET", path)
        if not isinstance(payload, list):
            raise ValueError("message list shape must be a JSON array")
        return payload

    async def get_status(self) -> dict[str, Any]:
        return await self._json("GET", "/session/status")

    async def open_sse(self, *, cursor: str | None = None) -> bytes:
        extra = {"cursor": cursor} if cursor else None
        observation_slice = min(
            self._config.request_timeout_seconds,
            self._config.poll_interval_seconds,
        )
        return await self._read(
            "GET",
            "/event",
            accept="text/event-stream",
            extra_params=extra,
            stream_deadline_seconds=observation_slice,
        )

    async def abort(self, session_id: str) -> dict[str, Any]:
        path = f"/session/{_path_segment(session_id, 'session id')}/abort"
        raw = await self._read("POST", path, body={}, accept="application/json")
        payload = json.loads(raw) if raw.strip() else True
        if payload is True:
            return {"ok": True}
        if isinstance(payload, dict):
            return payload
        raise ValueError("abort response must be a JSON object or true")

    async def get_session_diff(self, session_id: str) -> dict[str, Any] | list[Any] | None:
        path = f"/session/{_path_segment(session_id, 'session id')}/diff"
        try:
            payload = await self._json("GET", path)
        except httpx.HTTPStatusError as error:
            if error.response.status_code == 404:
                return None
            raise
        if not isinstance(payload, dict | list):
            raise ValueError("diff shape must be a JSON object or array")
        return payload

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
        extra_params: Mapping[str, str] | None = None,
        stream_deadline_seconds: float | None = None,
        _redirects: int = 0,
    ) -> bytes:
        params: dict[str, str] = {"directory": self._directory}
        if extra_params:
            params.update(extra_params)
        deadline = time.monotonic() + stream_deadline_seconds if stream_deadline_seconds is not None else None
        async with self._client.stream(
            method,
            path,
            headers={"Accept": accept},
            params=params,
            json=None if body is None else dict(body),
        ) as response:
            if response.status_code in {301, 302, 303, 307, 308}:
                if _redirects >= 1:
                    raise ValueError("redirect hop bound exceeded")
                location = response.headers.get("location")
                if not location:
                    raise ValueError("redirect is missing a location")
                absolute = urljoin(str(response.url), location)
                _RedirectTarget.model_validate({"location": absolute, "origin": self._origin})
                return await self._read(
                    method,
                    absolute,
                    body=body,
                    accept=accept,
                    extra_params=extra_params,
                    stream_deadline_seconds=stream_deadline_seconds,
                    _redirects=_redirects + 1,
                )
            response.raise_for_status()
            chunks: list[bytes] = []
            total = 0
            try:
                if deadline is None:
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > self._config.max_response_bytes:
                            raise ValueError("response exceeds max_response_bytes")
                        chunks.append(chunk)
                else:
                    async with asyncio.timeout(max(0.01, deadline - time.monotonic())):
                        async for chunk in response.aiter_bytes():
                            total += len(chunk)
                            if total > self._config.max_response_bytes:
                                raise ValueError("response exceeds max_response_bytes")
                            chunks.append(chunk)
                            if time.monotonic() >= deadline:
                                break
            except TimeoutError:
                if stream_deadline_seconds is None:
                    raise
            except httpx.TimeoutException:
                if stream_deadline_seconds is None:
                    raise
            return b"".join(chunks)
