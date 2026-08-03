"""Canonical L1/L2 data-knowledge artifact models (spec C4).

L1: `.aa/data-knowledge.yaml` — static domain knowledge only.
L2: `plans/data-knowledge.proposal.<layer>.yaml` — partial L1 + proposal metadata.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CapabilityKind = Literal["async_factory", "helper", "isolated_worker", "http"]
AuthMethod = Literal["token", "header", "login"]
ProposalMode = Literal["bootstrap", "delta"]


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
    model_config = ConfigDict(extra="forbid")

    symbol: str
    retry_safe: bool | None = None
    notes: str | None = None


class AuthLeaf(BaseModel):
    model_config = ConfigDict(extra="forbid")

    method: AuthMethod
    symbol: str | None = None
    acquire: dict[str, Any] | None = None
    notes: str | None = None


class AccountLeaf(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str
    role: str
    password_ref: str | None = None
    notes: str | None = None


class EntityLeaf(BaseModel):
    model_config = ConfigDict(extra="forbid")

    constraints: dict[str, Any] | None = None
    required_fields: list[str] | None = None
    notes: str | None = None

    @staticmethod
    def _validate_constraint_values(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key == "max_length" and (
                    isinstance(child, bool) or not isinstance(child, int) or child <= 0
                ):
                    raise ValueError("max_length must be a positive integer")
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
    def _at_least_one_constraint(self) -> "EntityLeaf":
        if not self.constraints and not self.required_fields:
            raise ValueError("at least one of constraints or required_fields is required")
        return self


class CapabilitiesAdapters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api: dict[str, dict[str, CapabilityLeaf]] = Field(default_factory=dict)
    e2e: dict[str, dict[str, CapabilityLeaf]] = Field(default_factory=dict)
    fuzz: dict[str, dict[str, CapabilityLeaf]] = Field(default_factory=dict)
    performance: dict[str, dict[str, CapabilityLeaf]] = Field(default_factory=dict)


class CapabilitiesBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    domain_factories: dict[str, dict[str, CapabilityLeaf]] = Field(default_factory=dict)
    adapters: CapabilitiesAdapters = Field(default_factory=CapabilitiesAdapters)
    cleanup: dict[str, CleanupLeaf] = Field(default_factory=dict)


class DataKnowledge(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int
    accounts: dict[str, AccountLeaf] = Field(default_factory=dict)
    auth: dict[str, AuthLeaf] = Field(default_factory=dict)
    entities: dict[str, EntityLeaf] = Field(default_factory=dict)
    capabilities: CapabilitiesBlock = Field(default_factory=CapabilitiesBlock)


class DataKnowledgeProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_version: str
    based_on_l1_version: int | None = None
    mode: ProposalMode
    accounts: dict[str, AccountLeaf] = Field(default_factory=dict)
    auth: dict[str, AuthLeaf] = Field(default_factory=dict)
    entities: dict[str, EntityLeaf] = Field(default_factory=dict)
    capabilities: CapabilitiesBlock = Field(default_factory=CapabilitiesBlock)
    discovered_candidates: list[dict[str, Any]] = Field(default_factory=list)
    needs_review: list[str] = Field(default_factory=list)
    promotion_checklist: list[str] = Field(default_factory=list)
