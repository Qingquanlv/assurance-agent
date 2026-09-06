"""Reviewed business assertions and their authoritative provenance."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Annotated, Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_serializer,
    field_validator,
    model_validator,
)

from graph_engine.frozen_json import freeze_json, thaw_json
from graph_engine.identifiers import IdentifierError, validate_qualified_id

from assurance_intake.contracts.workflow import EvidenceArtifactRefV1

if TYPE_CHECKING:
    from graph_engine.canonical import JSONValue
else:
    JSONValue = JsonValue


class LiteralExpectedV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["literal"]
    value: JSONValue

    @field_validator("value", mode="after")
    @classmethod
    def _freeze_value(cls, value: JSONValue) -> Any:
        return freeze_json(value)

    @field_serializer("value")
    def _serialize_value(self, value: object) -> Any:
        return thaw_json(value)


class InputExpectedV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["input"]
    key: Literal["username", "email", "is_active", "is_superuser", "dept_id"]


ExpectedValueV1 = Annotated[LiteralExpectedV1 | InputExpectedV1, Field(discriminator="kind")]


class BusinessAssertionV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    assertion_id: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    comparator: Literal["eq", "row_count_eq"]
    expected: ExpectedValueV1

    @field_validator("assertion_id")
    @classmethod
    def _assertion_id(cls, value: str) -> str:
        if re.fullmatch(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$", value) is None:
            raise ValueError("assertion_id must be a canonical dotted identifier")
        return value

    @field_validator("source_id")
    @classmethod
    def _source_id(cls, value: str) -> str:
        try:
            return validate_qualified_id(value)
        except IdentifierError as error:
            raise ValueError("source_id must be qualified") from error

    @field_validator("statement", "subject")
    @classmethod
    def _nonempty_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("assertion statement and subject must be non-empty")
        return value


class AssertionSourceV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: str
    origin: Literal["requirement", "reviewed_input"]
    reference: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    decision: Literal["accepted"]
    content_ref: EvidenceArtifactRefV1
    review_ref: EvidenceArtifactRefV1 | None = None

    @field_validator("source_id")
    @classmethod
    def _source_id(cls, value: str) -> str:
        try:
            return validate_qualified_id(value)
        except IdentifierError as error:
            raise ValueError("source_id must be qualified") from error

    @model_validator(mode="after")
    def _reviewed_input_has_review(self) -> Self:
        if self.origin == "reviewed_input" and self.review_ref is None:
            raise ValueError("reviewed_input source requires an authenticated review_ref")
        return self


class AssertionSourcesV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1"] = "1"
    case_id: str = Field(pattern=r"^[A-Za-z0-9_]+$")
    revision: str = Field(min_length=1)
    spec_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    sources: tuple[AssertionSourceV1, ...] = Field(min_length=1)

    @field_validator("sources")
    @classmethod
    def _unique_sources(
        cls,
        value: tuple[AssertionSourceV1, ...],
    ) -> tuple[AssertionSourceV1, ...]:
        identities = tuple(source.source_id for source in value)
        if len(identities) != len(set(identities)):
            raise ValueError("assertion source IDs must be unique")
        return value


def validate_assertion_provenance(
    *,
    case_id: str,
    revision: str,
    spec_digest: str,
    assertions: tuple[BusinessAssertionV1, ...],
    sources: AssertionSourcesV1,
    requirement_ref: EvidenceArtifactRefV1,
    authority_refs: tuple[EvidenceArtifactRefV1, ...],
) -> tuple[BusinessAssertionV1, ...]:
    """Authenticate one case's frozen assertions against its formal sidecar."""
    if sources.case_id != case_id or sources.revision != revision or sources.spec_digest != spec_digest:
        raise ValueError("assertion provenance does not match the case revision and digest")
    if not assertions:
        raise ValueError("business assertions must be non-empty")
    assertion_ids = tuple(assertion.assertion_id for assertion in assertions)
    if len(assertion_ids) != len(set(assertion_ids)):
        raise ValueError("business assertion IDs must be unique")
    source_ids = {source.source_id for source in sources.sources}
    missing = sorted(assertion.source_id for assertion in assertions if assertion.source_id not in source_ids)
    if missing:
        raise ValueError(f"business assertion has missing authoritative source: {missing[0]}")
    admitted = {(ref.path, ref.digest) for ref in authority_refs}
    requirement_identity = (requirement_ref.path, requirement_ref.digest)
    if requirement_identity not in admitted:
        raise ValueError("requirement_ref must be an independently authenticated authority ref")
    for source in sources.sources:
        content_identity = (source.content_ref.path, source.content_ref.digest)
        if source.origin == "requirement":
            if content_identity != requirement_identity:
                raise ValueError("requirement source must bind the authenticated requirement")
            if source.review_ref is not None:
                raise ValueError("requirement source cannot carry a review_ref")
            continue
        if content_identity not in admitted:
            raise ValueError("reviewed input source content_ref is not authenticated")
        if (
            source.review_ref is None
            or (
                source.review_ref.path,
                source.review_ref.digest,
            )
            not in admitted
        ):
            raise ValueError("reviewed input source review_ref is not authenticated")
    return assertions


__all__ = [
    "AssertionSourceV1",
    "AssertionSourcesV1",
    "BusinessAssertionV1",
    "ExpectedValueV1",
    "InputExpectedV1",
    "LiteralExpectedV1",
    "validate_assertion_provenance",
]
