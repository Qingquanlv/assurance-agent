"""Canonical L1/L2 data-knowledge artifact models (spec C4).

L1: `.aa/data-knowledge.yaml` — static domain knowledge only.
L2: `plans/data-knowledge.proposal.<layer>.yaml` — partial L1 + proposal metadata.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CapabilityKind = Literal["async_factory", "helper", "isolated_worker", "http"]
AuthMethod = Literal["token", "header", "login"]
ProposalMode = Literal["bootstrap", "delta"]
AuthExpectation = Literal["allow", "deny"]
LIST_AUTH_ROUTES_CAPABILITY = "list_auth_routes"
LIST_AUTH_ROUTES_KIND: CapabilityKind = "async_factory"


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


class AuthMatrixCell(BaseModel):
    """One declared authorization cell: route × method × auth token × expectation."""

    model_config = ConfigDict(extra="forbid")

    route: str
    method: str
    token: str
    expected: AuthExpectation
    allowed_status_codes: list[int] = Field(min_length=1)

    @field_validator("route", "method", "token")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must be a non-empty string")
        return value

    @field_validator("allowed_status_codes")
    @classmethod
    def _status_codes_are_http(cls, codes: list[int]) -> list[int]:
        for code in codes:
            if isinstance(code, bool) or not isinstance(code, int) or code < 100 or code > 599:
                raise ValueError("allowed_status_codes must be HTTP status integers")
        return codes


class EntityLeaf(BaseModel):
    model_config = ConfigDict(extra="forbid")

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
    def _at_least_one_constraint(self) -> "EntityLeaf":
        if not self.constraints and not self.required_fields:
            raise ValueError("at least one of constraints or required_fields is required")
        return self


class PersistedEntityLeaf(EntityLeaf):
    """Lossless read shape for historical L1 and Improvement records.

    New proposals use :class:`EntityLeaf` and therefore require a concrete,
    positive integer for every max-length constraint.  Existing canonical L1
    files may still contain the legacy boolean ``*_has_max_length: true`` flag;
    reading those files must preserve the value instead of rewriting history.
    """

    @field_validator("constraints")
    @classmethod
    def _known_constraint_values_are_typed(cls, constraints: dict[str, Any] | None) -> dict[str, Any] | None:
        return constraints


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


def assert_auth_matrix_tokens(
    auth: dict[str, AuthLeaf],
    auth_matrix: dict[str, AuthMatrixCell],
) -> None:
    for cell_id, cell in auth_matrix.items():
        if cell.token not in auth:
            raise ValueError(f"unknown auth key {cell.token!r} referenced by auth_matrix.{cell_id}")


def assert_auth_matrix_unique_cells(auth_matrix: dict[str, AuthMatrixCell]) -> None:
    """Fail when two cells share the same ``(route, method, token)`` identity."""
    seen: dict[tuple[str, str, str], str] = {}
    for cell_id, cell in auth_matrix.items():
        identity = (cell.route, cell.method, cell.token)
        prior = seen.get(identity)
        if prior is None:
            seen[identity] = cell_id
            continue
        raise ValueError(
            f"duplicate auth_matrix identity (route, method, token)="
            f"{identity!r} shared by {prior!r} and {cell_id!r}"
        )


def assert_list_auth_routes_contract(capabilities: CapabilitiesBlock) -> None:
    """Fail when ``list_auth_routes`` is declared but violates the A3 capability contract."""
    api_factories = capabilities.domain_factories.get("api") or {}
    leaf = api_factories.get(LIST_AUTH_ROUTES_CAPABILITY)
    if leaf is None:
        return
    if leaf.kind != LIST_AUTH_ROUTES_KIND:
        raise ValueError(
            f"capabilities.domain_factories.api.list_auth_routes kind must be "
            f"{LIST_AUTH_ROUTES_KIND!r}, got {leaf.kind!r}"
        )
    symbol = (leaf.symbol or "").strip()
    # Require exact symbol segment (not a lookalike suffix like evil_list_auth_routes).
    if not symbol or (
        symbol != LIST_AUTH_ROUTES_CAPABILITY and not symbol.endswith(f".{LIST_AUTH_ROUTES_CAPABILITY}")
    ):
        raise ValueError(
            "capabilities.domain_factories.api.list_auth_routes symbol must reference "
            f"{LIST_AUTH_ROUTES_CAPABILITY}"
        )


class DataKnowledge(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int
    accounts: dict[str, AccountLeaf] = Field(default_factory=dict)
    auth: dict[str, AuthLeaf] = Field(default_factory=dict)
    # L1 is a persisted artifact. Keep legacy constraint values readable and
    # lossless; strict authoring remains on DataKnowledgeProposal.entities.
    entities: dict[str, PersistedEntityLeaf] = Field(default_factory=dict)
    auth_matrix: dict[str, AuthMatrixCell] = Field(default_factory=dict)
    capabilities: CapabilitiesBlock = Field(default_factory=CapabilitiesBlock)

    @model_validator(mode="after")
    def _auth_matrix_and_list_auth_routes(self) -> "DataKnowledge":
        assert_auth_matrix_tokens(self.auth, self.auth_matrix)
        assert_auth_matrix_unique_cells(self.auth_matrix)
        assert_list_auth_routes_contract(self.capabilities)
        return self


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

    @model_validator(mode="after")
    def _auth_matrix_and_list_auth_routes(self) -> "DataKnowledgeProposal":
        assert_auth_matrix_tokens(self.auth, self.auth_matrix)
        assert_auth_matrix_unique_cells(self.auth_matrix)
        assert_list_auth_routes_contract(self.capabilities)
        return self


class PersistedDataKnowledgeProposal(BaseModel):
    """Typed persisted-read shape; unlike new Candidates, legacy constraints remain lossless."""

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
    """Cross the strict Candidate-to-persisted Improvement boundary explicitly."""
    if proposal is None:
        return None
    return PersistedDataKnowledgeProposal.model_validate(proposal.model_dump(mode="python"))
