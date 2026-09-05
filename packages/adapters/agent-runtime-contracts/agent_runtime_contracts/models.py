from __future__ import annotations

from collections.abc import Callable
from pathlib import PureWindowsPath
from typing import Any, Literal, Self

from pydantic import Field, JsonValue, field_serializer, field_validator, model_serializer, model_validator

from graph_engine.plugin_api import FrozenModel

from agent_runtime_contracts.schema import (
    bound_redacted_diagnostics,
    canonical_digest,
    canonical_json_bytes,
    freeze_json,
    reject_credentials_in_digest_input,
    thaw_json,
    validate_result_schema_document,
)


_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_ROUTING_MARKERS = (",", ";", "|", "->", "fallback", "route:", "candidates")
_NON_PORTABLE_SCOPE_CHARACTERS = frozenset('<>:"|?*')
_NON_PORTABLE_SCOPE_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)
JSONValue = JsonValue


class InstructionPart(FrozenModel):
    media_type: Literal["text/plain", "application/json"]
    text_content: str | None = None
    json_content: JSONValue | None = None
    digest: str = Field(pattern=_SHA256_PATTERN)

    @classmethod
    def text(cls, media_type: str, content: str) -> InstructionPart:
        return cls.model_validate(
            {
                "media_type": media_type,
                "text_content": content,
                "digest": canonical_digest(content),
            }
        )

    @classmethod
    def from_json(cls, content: object) -> InstructionPart:
        return cls.model_validate(
            {
                "media_type": "application/json",
                "json_content": content,
                "digest": canonical_digest(content),
            }
        )

    @field_validator("json_content", mode="after")
    @classmethod
    def _freeze_json_content(cls, value: JSONValue | None) -> Any:
        return freeze_json(value)

    @field_serializer("json_content")
    def _serialize_json_content(self, value: object) -> Any:
        return thaw_json(value)

    @model_validator(mode="after")
    def _validate_exclusive_content(self) -> Self:
        has_text = self.text_content is not None
        has_json = self.json_content is not None
        if has_text == has_json:
            raise ValueError("instruction part requires exactly one text or JSON content form")
        if self.media_type == "text/plain":
            if not has_text or not self.text_content:
                raise ValueError("text/plain requires non-empty text content")
            expected = canonical_digest(self.text_content)
        else:
            if not has_json:
                raise ValueError("application/json requires JSON content")
            expected = canonical_digest(thaw_json(self.json_content))
        if self.digest != expected:
            raise ValueError("instruction digest is not canonical")
        return self


class ResultContract(FrozenModel):
    schema_id: str = Field(min_length=1)
    schema_digest: str = Field(pattern=_SHA256_PATTERN)
    delivery_mode: Literal["assistant_json_local_v1"]
    schema_document: JSONValue | None = None

    @field_validator("schema_document", mode="after")
    @classmethod
    def _freeze_schema_document(cls, value: JSONValue | None) -> Any:
        return None if value is None else freeze_json(value)

    @field_serializer("schema_document")
    def _serialize_schema_document(self, value: object) -> Any:
        return None if value is None else thaw_json(value)

    @model_validator(mode="after")
    def _schema_document_matches_digest(self) -> Self:
        if self.schema_document is None:
            return self
        thawed = thaw_json(self.schema_document)
        if canonical_digest(thawed) != self.schema_digest:
            raise ValueError("result schema digest is not canonical")
        validate_result_schema_document(thawed)
        return self

    @model_serializer(mode="wrap")
    def _omit_missing_schema_document(self, serializer: Callable[[Any], dict[str, Any]]) -> dict[str, Any]:
        data = serializer(self)
        if data.get("schema_document") is None:
            data.pop("schema_document", None)
        return data


class ExecutionLimits(FrozenModel):
    max_seconds: int = Field(gt=0)


def _validate_project_relative_path(value: str) -> str:
    windows_path = PureWindowsPath(value)
    if (
        not value
        or "\\" in value
        or value.startswith("/")
        or windows_path.is_absolute()
        or bool(windows_path.drive)
    ):
        raise ValueError("path must be a canonical project-relative path")
    if any(segment in {"", ".", ".."} for segment in value.split("/")):
        raise ValueError("path must not contain absolute, parent, or dot segments")
    return value


def _validate_scope_id(value: str) -> str:
    basename = value.split(".", 1)[0].upper()
    if (
        not value
        or value != value.strip()
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or value[-1] == "."
        or basename in _NON_PORTABLE_SCOPE_NAMES
        or any(character in _NON_PORTABLE_SCOPE_CHARACTERS for character in value)
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("scope_id must be one safe portable path component")
    return value


BoundedAgentProfile = Literal[
    "assurance-v1-archiver",
    "assurance-v1-doc-author",
    "assurance-v1-executor",
    "assurance-v1-explorer",
    "assurance-v1-reporter",
    "assurance-v1-reviewer",
    "assurance-v1-test-author",
]


class AgentWorkspaceV1(FrozenModel):
    schema_version: Literal["1"] = "1"
    agent_profile: BoundedAgentProfile
    scope_id: str
    write_root: str
    allowed_outputs: tuple[str, ...]
    read_roots: tuple[str, ...] = ()
    identity_digest: str = Field(pattern=_SHA256_PATTERN)

    @field_validator("write_root")
    @classmethod
    def _validate_write_root(cls, value: str) -> str:
        return _validate_project_relative_path(value)

    @field_validator("scope_id")
    @classmethod
    def _scope_id(cls, value: str) -> str:
        return _validate_scope_id(value)

    @field_validator("allowed_outputs")
    @classmethod
    def _validate_allowed_outputs(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(_validate_project_relative_path(value) for value in values)
        if normalized != tuple(sorted(normalized)):
            raise ValueError("allowed outputs must be sorted exact logical paths")
        if len(set(normalized)) != len(normalized):
            raise ValueError("allowed outputs must be unique exact logical paths")
        return normalized

    @field_validator("read_roots")
    @classmethod
    def _validate_read_roots(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(_validate_project_relative_path(value) for value in values)
        if normalized != tuple(sorted(normalized)):
            raise ValueError("read roots must be sorted exact project-relative paths")
        if len(set(normalized)) != len(normalized):
            raise ValueError("read roots must be unique exact project-relative paths")
        return normalized

    @model_validator(mode="after")
    def _authenticate_identity_digest(self) -> Self:
        expected = canonical_digest(self.model_dump(mode="json", exclude={"identity_digest"}))
        if self.identity_digest != expected:
            raise ValueError("workspace identity digest is not canonical")
        return self


class FrozenExecutionSelection(FrozenModel):
    provider_model: str = Field(min_length=1)
    worker_profile: str = Field(min_length=1)
    permission_profile_digest: str = Field(pattern=_SHA256_PATTERN)
    limits: ExecutionLimits

    @field_validator("provider_model")
    @classmethod
    def _validate_provider_model(cls, value: str) -> str:
        if value != value.strip() or any(character.isspace() for character in value):
            raise ValueError("provider_model must be one exact selection")
        lowered = value.lower()
        if any(marker in lowered for marker in _ROUTING_MARKERS):
            raise ValueError("provider_model must not contain routing, fallbacks, or candidate lists")
        return value


class AgentRunRequest(FrozenModel):
    schema_version: Literal["1"] = "1"
    instructions: tuple[InstructionPart, ...] = Field(min_length=1)
    result_contract: ResultContract
    execution: FrozenExecutionSelection
    workspace: AgentWorkspaceV1
    request_policy_digest: str = Field(pattern=_SHA256_PATTERN)
    request_config_digest: str = Field(pattern=_SHA256_PATTERN)

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.model_dump(mode="json"))


class AgentRunResult(FrozenModel):
    schema_version: Literal["1"] = "1"
    result_payload: JSONValue
    result_digest: str = Field(pattern=_SHA256_PATTERN)
    evidence_digest: str = Field(pattern=_SHA256_PATTERN)
    provider_diff_digest: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    adapter_id: str = Field(min_length=1)
    adapter_version: str = Field(min_length=1)
    diagnostics: tuple[str, ...] = ()

    @field_validator("result_payload", mode="after")
    @classmethod
    def _freeze_result_payload(cls, value: JSONValue) -> Any:
        return freeze_json(value)

    @field_serializer("result_payload")
    def _serialize_result_payload(self, value: object) -> Any:
        return thaw_json(value)

    @field_validator("diagnostics")
    @classmethod
    def _bound_and_redact_diagnostics(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return bound_redacted_diagnostics(value)

    @model_validator(mode="after")
    def _authenticate_result_digest(self) -> Self:
        thawed = thaw_json(self.result_payload)
        reject_credentials_in_digest_input(thawed)
        expected = canonical_digest(thawed)
        if self.result_digest != expected:
            raise ValueError("result digest is not canonical")
        return self

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.model_dump(mode="json"))
