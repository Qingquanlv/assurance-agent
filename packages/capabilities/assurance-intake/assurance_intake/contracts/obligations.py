"""Sourced verification obligations: the only formal expected-behavior definition."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from graph_engine.plugin_api import FrozenModel

from assurance_intake.contracts.common import MrcCategory, MrcLayer
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1

SourceKind = Literal[
    "requirement",
    "contract",
    "decision",
    "code",
    "case",
    "factory",
    "issue",
    "risk_check",
]
_NORMATIVE_SOURCE_KINDS: frozenset[str] = frozenset({"requirement", "contract", "decision"})
_EXCLUSION_SOURCE_KINDS: frozenset[str] = frozenset({"requirement", "decision"})


class SourceRefV1(FrozenModel):
    kind: SourceKind
    artifact: EvidenceArtifactRefV1
    locator: str = Field(min_length=1)


class ExpectedBasisV1(FrozenModel):
    source: SourceRefV1
    source_status: Literal["authenticated", "pending"]

    @model_validator(mode="after")
    def _authenticated_requires_normative_kind(self) -> Self:
        if self.source_status == "authenticated" and self.source.kind not in _NORMATIVE_SOURCE_KINDS:
            raise ValueError("authenticated expected basis requires normative authority")
        return self


class RequiredObservationV1(FrozenModel):
    observation_key: str = Field(min_length=1)
    condition: str = Field(min_length=1)
    predicate: Literal["status_code_eq"]
    expected: int | None
    basis_refs: tuple[SourceRefV1, ...]

    @field_validator("expected")
    @classmethod
    def _http_status_or_none(cls, value: int | None) -> int | None:
        if value is None:
            return value
        if value < 100 or value > 599:
            raise ValueError("expected status must be an HTTP status code 100-599")
        return value


class VerificationRequirementV1(FrozenModel):
    requirement_id: str = Field(min_length=1)
    profile_id: str = Field(min_length=1)
    prerequisites: tuple[str, ...]
    observations: tuple[RequiredObservationV1, ...]
    semantic_review_required: bool
    subject_binding_required: bool

    @model_validator(mode="after")
    def _unique_observation_keys(self) -> Self:
        keys = tuple(item.observation_key for item in self.observations)
        if len(keys) != len(set(keys)):
            raise ValueError("observation_key must be unique within a verification requirement")
        return self


class PreparedObligationV1(FrozenModel):
    mrc_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
    key: str | None
    proposed_key: str | None
    category: MrcCategory
    layer: MrcLayer
    statement: str = Field(min_length=1)
    applicability_conditions: tuple[str, ...]
    expected_basis_refs: tuple[ExpectedBasisV1, ...]
    # Local ids only. Full identity is (plan_digest, inventory digest, row_id) at read time.
    impact_row_ids: tuple[str, ...]
    required: bool
    scope_disposition: Literal["included", "excluded"]
    exclusion_basis: SourceRefV1 | None
    open_questions: tuple[str, ...]
    verification_requirements: tuple[VerificationRequirementV1, ...]

    @model_validator(mode="after")
    def _scope_matches_required(self) -> Self:
        if self.scope_disposition == "included":
            if not self.required:
                raise ValueError("included obligations must stay required")
            if self.exclusion_basis is not None:
                raise ValueError("included obligations cannot carry an exclusion basis")
            return self
        if self.required:
            raise ValueError("excluded obligations must set required=False")
        if self.exclusion_basis is None or self.exclusion_basis.kind not in _EXCLUSION_SOURCE_KINDS:
            raise ValueError("exclusion requires an independent requirement or decision basis")
        return self


class GoalSummaryV1(FrozenModel):
    subject: str = Field(min_length=1)
    stage: Literal["implementation", "release", "production"]
    scope: tuple[str, ...]
    preconditions: tuple[str, ...]
    assumptions: tuple[str, ...]
    out_of_scope: tuple[str, ...]
    source_refs: tuple[SourceRefV1, ...]
    open_questions: tuple[str, ...]


class DiscoveryAuditRowV1(FrozenModel):
    source: SourceRefV1
    origin: Literal["requirement", "diff", "requirement_hint", "risk_check"]
    disposition: Literal["mapped", "excluded", "pending", "uninspected"]
    mrc_ids: tuple[str, ...]
    reason: str = Field(min_length=1)
    # Host projection. Agent-reported completeness is not accepted at authenticate time.
    read_state: Literal["complete", "unread", "truncated"]
    validity: Literal["current", "stale", "unknown"]
