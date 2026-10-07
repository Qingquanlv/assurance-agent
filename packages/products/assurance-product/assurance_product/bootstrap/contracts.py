from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Annotated, Any, Literal

from pydantic import ConfigDict, Field, field_validator

from graph_engine.plugin_api import FrozenModel

from assurance_product.models import FAMILY_NONEMPTY_ENTRYPOINTS, TEST_FAMILY_ORDER, BusinessBudgetsV1

BootstrapPhase = Literal[
    "preparing",
    "opencode_ready",
    "compiled",
    "started",
    "running",
    "terminal",
]

_ENV_NAME = r"^[A-Za-z_][A-Za-z0-9_]*$"
_ROUTING_MARKERS = (",", ";", "|", "->", "fallback", "route:", "candidates")


class SutEndpointV1(FrozenModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    base_url: str = Field(min_length=1)
    readiness_url: str = Field(min_length=1)
    env: Mapping[str, str] = Field(default_factory=dict)
    env_from_node: tuple[str, ...] = ()
    api_base_url: str | None = None
    ui_base_url: str | None = None
    ui_paths: tuple[Annotated[str, Field(pattern=r"^/")], ...] = ()

    @field_validator("env_from_node")
    @classmethod
    def _env_names(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for name in value:
            if "=" in name or not re.fullmatch(_ENV_NAME, name):
                raise ValueError("env_from_node entries must be env names, not assignments")
        if len(set(value)) != len(value):
            raise ValueError("env_from_node must be unique")
        return value

    @field_validator("env")
    @classmethod
    def _env_keys(cls, value: Mapping[str, str]) -> Mapping[str, str]:
        for name in value:
            if not re.fullmatch(_ENV_NAME, name):
                raise ValueError(f"sut.env key is not an env name: {name}")
        return value


class RouteDefaultsV1(FrozenModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    provider_model: str = Field(min_length=1)
    worker_profile: str = Field(min_length=1)

    @field_validator("provider_model", "worker_profile")
    @classmethod
    def _exact(cls, value: str) -> str:
        if value != value.strip() or any(character.isspace() for character in value):
            raise ValueError("route field must be one exact token")
        lowered = value.lower()
        if any(marker in lowered for marker in _ROUTING_MARKERS):
            raise ValueError("route field must not contain routing markers")
        return value


class RunSpecV1(FrozenModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal["1"]
    product: Literal["assurance-opencode"]
    entrypoint: str
    requirement: str = Field(min_length=1)
    candidate_test_families: tuple[Literal["api", "e2e", "fuzz", "performance"], ...]
    case_modules: tuple[str, ...]
    sut: SutEndpointV1
    routes: RouteDefaultsV1
    budgets: BusinessBudgetsV1
    opencode_token_env: str = Field(pattern=_ENV_NAME)
    timeout_seconds: int = Field(gt=0, le=86400)

    @field_validator("entrypoint")
    @classmethod
    def _entrypoint(cls, value: str) -> str:
        if value not in FAMILY_NONEMPTY_ENTRYPOINTS:
            raise ValueError("entrypoint must be full or intake")
        return value

    @field_validator("candidate_test_families")
    @classmethod
    def _families(
        cls, value: tuple[Literal["api", "e2e", "fuzz", "performance"], ...]
    ) -> tuple[Literal["api", "e2e", "fuzz", "performance"], ...]:
        if len(set(value)) != len(value):
            raise ValueError("candidate_test_families must be unique")
        order = {name: index for index, name in enumerate(TEST_FAMILY_ORDER)}
        if tuple(sorted(value, key=order.__getitem__)) != value:
            raise ValueError("candidate_test_families must be in canonical family order")
        return value

    @field_validator("case_modules")
    @classmethod
    def _modules(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for module in value:
            if not module or module.startswith("/") or ".." in module.split("/"):
                raise ValueError(f"invalid case module: {module}")
        return value


class OpenCodeHandleV1(FrozenModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    endpoint: str
    pid: int | None = None
    process_identity: dict[str, Any] | None = None
    ownership: Literal["private", "shared"] = "private"


class BootstrapStatusV1(FrozenModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    phase: BootstrapPhase
    change_id: str
    opencode: OpenCodeHandleV1 | None = None
    root_session_id: str | None = None
    status: Mapping[str, Any] = Field(default_factory=dict)
    started_at: str | None = None
    updated_at: str | None = None
    ended_at: str | None = None
    exit_code: int | None = None
    error: str | None = None
