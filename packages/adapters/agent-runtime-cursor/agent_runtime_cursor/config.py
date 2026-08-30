from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, ValidationError, field_validator, model_validator

from graph_engine.plugin_api import FrozenModel, TaskRequest

from agent_runtime_cursor.redaction import redact_validation_error


_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_SHELL_FRAGMENTS = (
    ";",
    "|",
    "&",
    "$",
    "`",
    "(",
    ")",
    "<",
    ">",
    "*",
    "?",
    "[",
    "]",
    "{",
    "}",
    "~",
    "\n",
    "\r",
)
_ALLOWED_ENVIRONMENT_NAMES = frozenset({"PATH", "CURSOR_API_KEY"})
PROTOCOL_PROFILE = "confined_process"


class AdapterConfigurationError(ValueError):
    """Raised when locked adapter binding data is missing or invalid."""


def _reject_shell_fragments(value: str, label: str) -> str:
    if value != value.strip() or any(character.isspace() for character in value):
        raise ValueError(f"{label} must be a single token")
    if any(fragment in value for fragment in _SHELL_FRAGMENTS):
        raise ValueError(f"{label} must not contain shell fragments")
    return value


class CursorAdapterConfig(FrozenModel):
    schema_version: Literal["1"] = "1"
    executable: str
    executable_digest: str = Field(pattern=_SHA256_PATTERN)
    expected_version: str = Field(min_length=1)
    protocol_profile: Literal["confined_process"] = PROTOCOL_PROFILE
    secret_handle: str | None = None
    environment_names: tuple[str, ...]
    graceful_cancel_seconds: float = Field(gt=0, le=60)
    forced_cancel_seconds: float = Field(gt=0, le=60)
    max_output_bytes: int = Field(gt=0, le=16_000_000)
    max_line_bytes: int = Field(gt=0, le=1_000_000)
    adapter_configuration_digest: str = Field(pattern=_SHA256_PATTERN)

    @field_validator("executable")
    @classmethod
    def _validate_executable(cls, value: str) -> str:
        _reject_shell_fragments(value, "executable")
        if value.startswith("\\\\") or not value.startswith("/"):
            raise ValueError("executable must be an exact absolute path")
        if any(part in {".", ".."} for part in value.split("/") if part):
            raise ValueError("executable must not contain relative path segments")
        return value

    @field_validator("expected_version")
    @classmethod
    def _validate_expected_version(cls, value: str) -> str:
        return _reject_shell_fragments(value, "expected_version")

    @field_validator("secret_handle")
    @classmethod
    def _validate_secret_handle(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value != value.strip() or any(character.isspace() for character in value):
            raise ValueError("secret_handle must be a single handle token")
        if any(marker in value for marker in (":", "/", "\\", "@")):
            raise ValueError("secret_handle must not contain credentials or paths")
        return value

    @field_validator("environment_names")
    @classmethod
    def _validate_environment_names(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("environment_names must be unique")
        unknown = [name for name in value if name not in _ALLOWED_ENVIRONMENT_NAMES]
        if unknown:
            raise ValueError("environment_names must be the closed PATH/CURSOR_API_KEY allowlist")
        return value

    @model_validator(mode="after")
    def _authenticate_secret_injection(self) -> CursorAdapterConfig:
        injects_key = "CURSOR_API_KEY" in self.environment_names
        if injects_key != (self.secret_handle is not None):
            raise ValueError("CURSOR_API_KEY injection requires secret_handle and nothing else")
        return self

    @classmethod
    def from_request(cls, request: TaskRequest) -> Self:
        try:
            return cls.model_validate(request.binding_data)
        except ValidationError as error:
            raise AdapterConfigurationError(redact_validation_error(error)) from error
