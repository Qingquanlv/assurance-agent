from __future__ import annotations

from typing import Any, Literal, Self

from pydantic import Field, JsonValue, field_serializer, field_validator, model_validator

from graph_engine.plugin_api import FrozenModel

from agent_runtime_contracts.schema import (
    bound_redacted_diagnostics,
    canonical_digest,
    canonical_json_bytes,
    freeze_json,
    reject_credentials_in_digest_input,
    thaw_json,
)


_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_ROUTING_MARKERS = (",", ";", "|", "->", "fallback", "route:", "candidates")
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
    extraction_mode: Literal["structured"]


class ExecutionLimits(FrozenModel):
    max_seconds: int = Field(gt=0)


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
    request_policy_digest: str = Field(pattern=_SHA256_PATTERN)
    request_config_digest: str = Field(pattern=_SHA256_PATTERN)

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.model_dump(mode="json"))


class AgentRunResult(FrozenModel):
    schema_version: Literal["1"] = "1"
    structured_result: JSONValue
    result_digest: str = Field(pattern=_SHA256_PATTERN)
    evidence_digest: str = Field(pattern=_SHA256_PATTERN)
    provider_diff_digest: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    adapter_id: str = Field(min_length=1)
    adapter_version: str = Field(min_length=1)
    diagnostics: tuple[str, ...] = ()

    @field_validator("structured_result", mode="after")
    @classmethod
    def _freeze_structured_result(cls, value: JSONValue) -> Any:
        return freeze_json(value)

    @field_serializer("structured_result")
    def _serialize_structured_result(self, value: object) -> Any:
        return thaw_json(value)

    @field_validator("diagnostics")
    @classmethod
    def _bound_and_redact_diagnostics(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return bound_redacted_diagnostics(value)

    @model_validator(mode="after")
    def _authenticate_result_digest(self) -> Self:
        thawed = thaw_json(self.structured_result)
        reject_credentials_in_digest_input(thawed)
        expected = canonical_digest(thawed)
        if self.result_digest != expected:
            raise ValueError("result digest is not canonical")
        return self

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.model_dump(mode="json"))
