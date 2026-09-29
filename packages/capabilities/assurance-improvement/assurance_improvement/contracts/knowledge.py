"""Leaf schema for a domain-knowledge delta.

Promotion of a persisted delta into `.aa/data-knowledge.yaml` lives in
`assurance_improvement.operations.knowledge_promote`.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CapabilityKind = Literal["async_factory", "helper", "isolated_worker", "http"]
AuthMethod = Literal["token", "header", "login"]
ProposalMode = Literal["bootstrap", "delta"]
AuthExpectation = Literal["allow", "deny"]

_FROZEN = ConfigDict(extra="forbid")


class CapabilityLeaf(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    kind: CapabilityKind
    symbol: str
    entity: str | None = None
    returns: dict[str, str] | None = None
    cleanup_ref: str | None = None
    retry_safe: bool | None = None
    notes: str | None = None
    create_if_missing: bool | None = Field(default=None, alias="create-if-missing")
    acquire: dict[str, Any] | None = None
    transport: str | None = None


class CleanupLeaf(BaseModel):
    model_config = _FROZEN

    symbol: str
    retry_safe: bool | None = None
    notes: str | None = None


class AuthLeaf(BaseModel):
    model_config = _FROZEN

    method: AuthMethod
    symbol: str | None = None
    acquire: dict[str, Any] | None = None
    notes: str | None = None


class AccountLeaf(BaseModel):
    model_config = _FROZEN

    username: str
    role: str
    password_ref: str | None = None
    notes: str | None = None


class AuthMatrixCell(BaseModel):
    model_config = _FROZEN

    route: str
    method: str
    token: str
    expected: AuthExpectation
    allowed_status_codes: list[int] = Field(min_length=1)


class EntityLeaf(BaseModel):
    model_config = _FROZEN

    constraints: dict[str, Any] | None = None
    required_fields: list[str] | None = None
    notes: str | None = None

    @staticmethod
    def _is_max_length_key(key: str) -> bool:
        return key == "max_length" or key.endswith("_has_max_length")

    @staticmethod
    def _validate_max_length_value(value: Any) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError("max_length must be a positive integer")

    @staticmethod
    def _validate_constraint_values(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if EntityLeaf._is_max_length_key(str(key)):
                    EntityLeaf._validate_max_length_value(child)
                else:
                    EntityLeaf._validate_constraint_values(child)
        elif isinstance(value, list | tuple):
            for child in value:
                EntityLeaf._validate_constraint_values(child)

    @field_validator("constraints")
    @classmethod
    def _known_constraint_values_are_typed(cls, constraints: dict[str, Any] | None) -> dict[str, Any] | None:
        cls._validate_constraint_values(constraints)
        return constraints

    @model_validator(mode="after")
    def _at_least_one_constraint(self) -> EntityLeaf:
        if not self.constraints and not self.required_fields:
            raise ValueError("at least one of constraints or required_fields is required")
        return self


class PersistedEntityLeaf(EntityLeaf):
    @field_validator("constraints")
    @classmethod
    def _known_constraint_values_are_typed(cls, constraints: dict[str, Any] | None) -> dict[str, Any] | None:
        return constraints


class CapabilitiesAdapters(BaseModel):
    model_config = _FROZEN

    api: dict[str, dict[str, CapabilityLeaf]] = Field(default_factory=dict)
    e2e: dict[str, dict[str, CapabilityLeaf]] = Field(default_factory=dict)
    fuzz: dict[str, dict[str, CapabilityLeaf]] = Field(default_factory=dict)
    performance: dict[str, dict[str, CapabilityLeaf]] = Field(default_factory=dict)


class CapabilitiesBlock(BaseModel):
    model_config = _FROZEN

    domain_factories: dict[str, dict[str, CapabilityLeaf]] = Field(default_factory=dict)
    adapters: CapabilitiesAdapters = Field(default_factory=CapabilitiesAdapters)
    cleanup: dict[str, CleanupLeaf] = Field(default_factory=dict)


class DataKnowledgeProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_version: str
    based_on_l1_version: int | None = None
    mode: ProposalMode
    accounts: dict[str, AccountLeaf] = Field(default_factory=dict)
    auth: dict[str, AuthLeaf] = Field(default_factory=dict)
    entities: dict[str, EntityLeaf] = Field(default_factory=dict)
    auth_matrix: dict[str, AuthMatrixCell] = Field(default_factory=dict)
    capabilities: CapabilitiesBlock = Field(default_factory=CapabilitiesBlock)
    discovered_candidates: list[dict[str, Any]] = Field(default_factory=list)
    needs_review: list[str] = Field(default_factory=list)
    promotion_checklist: list[str] = Field(default_factory=list)


class PersistedDataKnowledgeProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_version: str
    based_on_l1_version: int | None = None
    mode: ProposalMode
    accounts: dict[str, AccountLeaf] = Field(default_factory=dict)
    auth: dict[str, AuthLeaf] = Field(default_factory=dict)
    entities: dict[str, PersistedEntityLeaf] = Field(default_factory=dict)
    auth_matrix: dict[str, AuthMatrixCell] = Field(default_factory=dict)
    capabilities: CapabilitiesBlock = Field(default_factory=CapabilitiesBlock)
    discovered_candidates: list[dict[str, Any]] = Field(default_factory=list)
    needs_review: list[str] = Field(default_factory=list)
    promotion_checklist: list[str] = Field(default_factory=list)


def to_persisted_data_knowledge_proposal(
    proposal: DataKnowledgeProposal | None,
) -> PersistedDataKnowledgeProposal | None:
    if proposal is None:
        return None
    return PersistedDataKnowledgeProposal.model_validate(proposal.model_dump(mode="python"))
