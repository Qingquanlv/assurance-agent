"""Open one authenticated OpenCode HTTP client for a task invocation."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx

from graph_engine.plugin_api import SecretHandleUnauthorized, TaskContext

from agent_runtime_opencode.config import OpenCodeAdapterConfig
from agent_runtime_opencode.protocol import OpenCodeHttpClient

PROVIDER_ERRORS: tuple[type[Exception], ...] = (
    httpx.TransportError,
    httpx.HTTPStatusError,
    json.JSONDecodeError,
    ValueError,
)


@dataclass(frozen=True, slots=True)
class Connection:
    client: OpenCodeHttpClient
    secret: bytes


@asynccontextmanager
async def open_client(config: OpenCodeAdapterConfig, context: TaskContext) -> AsyncIterator[Connection]:
    if context.secrets is None:
        raise SecretHandleUnauthorized("secret port is required")
    secret = context.secrets.resolve(config.secret_handle)
    client = OpenCodeHttpClient(config, secret=secret, directory=str(context.project_root.resolve()))
    try:
        yield Connection(client=client, secret=secret)
    finally:
        await client.aclose()
