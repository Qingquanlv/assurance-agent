from __future__ import annotations

from typing import Literal
from urllib.parse import urlparse

from pydantic import AnyHttpUrl, Field, field_validator

from graph_engine.plugin_api import FrozenModel


_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_PROTOCOL_PROFILE = "opencode-http-v1"


def endpoint_origin(url: str) -> str:
    parsed = urlparse(url)
    if parsed.username or parsed.password:
        raise ValueError("endpoint must not contain URL credentials")
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("endpoint must be an absolute HTTP origin")
    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    return f"{parsed.scheme}://{parsed.hostname}:{port}"


class OpenCodeAdapterConfig(FrozenModel):
    schema_version: Literal["1"] = "1"
    endpoint: AnyHttpUrl
    tls_identity_digest: str = Field(pattern=_SHA256_PATTERN)
    secret_handle: str = Field(min_length=1)
    protocol_profile: Literal["opencode-http-v1"] = _PROTOCOL_PROFILE
    project_scope: str = Field(min_length=1)
    request_timeout_seconds: float = Field(gt=0, le=300)
    observation_horizon_seconds: float = Field(gt=0, le=3600)
    max_response_bytes: int = Field(gt=0, le=4_000_000)

    @field_validator("endpoint")
    @classmethod
    def _reject_url_credentials(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        endpoint_origin(str(value))
        if value.username or value.password:
            raise ValueError("endpoint must not contain URL credentials")
        return value

    @field_validator("secret_handle")
    @classmethod
    def _validate_secret_handle(cls, value: str) -> str:
        if value != value.strip() or any(character.isspace() for character in value):
            raise ValueError("secret_handle must be a single handle token")
        if any(marker in value for marker in (":", "/", "\\", "@")):
            raise ValueError("secret_handle must not contain credentials or paths")
        return value

    @field_validator("project_scope")
    @classmethod
    def _validate_project_scope(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("project_scope must not include surrounding whitespace")
        return value

    @property
    def origin(self) -> str:
        return endpoint_origin(str(self.endpoint))
