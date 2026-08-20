from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field as dataclass_field
from pathlib import Path, PureWindowsPath
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypeVar

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_serializer,
    field_validator,
    model_serializer,
    model_validator,
)

from graph_engine.errors import GraphEngineError
from graph_engine.frozen_json import FrozenJSONValue, freeze_json, thaw_json
from graph_engine.identifiers import IdentifierError, validate_qualified_id

if TYPE_CHECKING:
    from graph_engine.canonical import JSONValue
else:
    JSONValue = JsonValue


class PluginContractError(GraphEngineError):
    """Raised when a frozen Phase 2 plugin contribution violates its descriptor."""


FailureKind = Literal[
    "transient", "timeout", "invalid_input", "invalid_output", "external_effect", "internal"
]
TaskStatus = Literal["succeeded", "failed", "stopped"]

_FROZEN_MODEL_CONFIG = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
_Capability = TypeVar("_Capability")


class FrozenModel(BaseModel):
    model_config = _FROZEN_MODEL_CONFIG


class TaskFailure(FrozenModel):
    kind: FailureKind
    message: str
    retryable: bool = True


class EffectIntent(FrozenModel):
    kind: str
    payload: JSONValue

    @field_validator("kind")
    @classmethod
    def _validate_kind(cls, value: str) -> str:
        try:
            return validate_qualified_id(value)
        except IdentifierError as error:
            raise ValueError(f"invalid effect kind: {value!r}") from error


class TaskRequest(FrozenModel):
    invocation_id: str
    task_id: str
    graph_instance_id: str
    node_id: str
    capability_id: str
    target_capability_id: str | None = None
    binding_data: JSONValue = None
    resource_ids: tuple[str, ...] = ()
    attempt: int = Field(ge=1)
    input: JSONValue
    prior_failure: TaskFailure | None = None

    @field_validator("target_capability_id")
    @classmethod
    def _validate_target_capability_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_contract_id(value, "target capability id")

    @field_validator("binding_data", mode="after")
    @classmethod
    def _freeze_binding_data(cls, value: JSONValue) -> Any:
        return freeze_json(value)

    @field_validator("resource_ids")
    @classmethod
    def _validate_resource_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        validated = tuple(_validate_contract_id(value, "request resource id") for value in values)
        if len(set(validated)) != len(validated):
            raise ValueError("request resource ids must be unique")
        return validated

    @field_serializer("binding_data")
    def _serialize_binding_data(self, value: object) -> Any:
        return thaw_json(value)


class TaskOutcome(FrozenModel):
    status: TaskStatus
    output: JSONValue = None
    failure: TaskFailure | None = None
    stop_reason: str | None = None
    effects: tuple[EffectIntent, ...] = ()

    @model_validator(mode="after")
    def _validate_status_fields(self) -> TaskOutcome:
        if (self.failure is not None) != (self.status == "failed"):
            raise ValueError("failure is required exactly when status is failed")
        if self.status == "stopped":
            if not self.stop_reason:
                raise ValueError("a non-empty stop_reason is required when status is stopped")
        elif self.stop_reason is not None:
            raise ValueError("stop_reason is allowed only when status is stopped")
        if self.status != "succeeded" and self.effects:
            raise ValueError("effects are allowed only when status is succeeded")
        return self

    @model_serializer(mode="wrap")
    def _serialize(self, handler: Any) -> Any:
        serialized = handler(self)
        if not self.effects:
            serialized.pop("effects", None)
        return serialized

    @classmethod
    def succeeded(cls, output: JSONValue = None, *, effects: tuple[EffectIntent, ...] = ()) -> TaskOutcome:
        return cls(status="succeeded", output=output, effects=tuple(effects))

    @classmethod
    def failed(cls, kind: FailureKind, message: str, *, retryable: bool = True) -> TaskOutcome:
        return cls(
            status="failed",
            failure=TaskFailure(kind=kind, message=message, retryable=retryable),
        )

    @classmethod
    def stopped(cls, reason: str, output: JSONValue = None) -> TaskOutcome:
        return cls(status="stopped", output=output, stop_reason=reason)


class EffectPolicy(FrozenModel):
    max_attempts: int = Field(ge=1)
    timeout_seconds: float = Field(gt=0)
    backoff_seconds: float = Field(ge=0)


class EffectApplyResult(FrozenModel):
    status: Literal["applied", "transient", "permanent"]
    receipt: JSONValue = None
    failure: TaskFailure | None = None

    @model_validator(mode="after")
    def _validate_status_fields(self) -> EffectApplyResult:
        if self.status == "applied":
            if self.failure is not None:
                raise ValueError("failure is allowed only when effect application did not apply")
        elif self.receipt is not None or self.failure is None:
            raise ValueError("receipt and failure are mutually exclusive for effect application results")
        elif self.status == "permanent" and self.failure.retryable:
            raise ValueError("permanent effect failure must not be retryable")
        return self

    @classmethod
    def applied(cls, receipt: JSONValue) -> EffectApplyResult:
        return cls(status="applied", receipt=receipt)


class EffectReconcileResult(FrozenModel):
    status: Literal["not_applied", "pending", "applied", "permanently_failed"]
    receipt: JSONValue = None
    failure: TaskFailure | None = None

    @model_validator(mode="after")
    def _validate_status_fields(self) -> EffectReconcileResult:
        if self.status == "applied":
            if self.failure is not None:
                raise ValueError("failure is not allowed when effect reconciliation applied")
        elif self.status == "permanently_failed":
            if self.receipt is not None or self.failure is None:
                raise ValueError(
                    "receipt and failure are mutually exclusive for effect reconciliation results"
                )
            if self.failure.retryable:
                raise ValueError("permanent effect failure must not be retryable")
        elif self.receipt is not None or self.failure is not None:
            raise ValueError(
                "receipt and failure are allowed only for terminal effect reconciliation results"
            )
        return self

    @classmethod
    def applied(cls, receipt: JSONValue) -> EffectReconcileResult:
        return cls(status="applied", receipt=receipt)


class DurableEffectHandler(Protocol):
    async def apply(self, intent: EffectIntent, idempotency_key: str) -> EffectApplyResult: ...

    async def reconcile(self, intent: EffectIntent, idempotency_key: str) -> EffectReconcileResult: ...


@dataclass(frozen=True, slots=True)
class TaskContext:
    workspace_root: Path
    heartbeat: Callable[[], None]

    def effect(self, kind: str, payload: JSONValue) -> EffectIntent:
        return EffectIntent(kind=kind, payload=payload)


class TaskHandler(Protocol):
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome: ...


def _validate_resource_prefix(value: str) -> str:
    windows_path = PureWindowsPath(value)
    if (
        not value
        or "\\" in value
        or value.startswith("/")
        or windows_path.is_absolute()
        or bool(windows_path.drive)
    ):
        raise ValueError("resource must be a non-empty relative resource prefix")
    if any(segment in {"", ".", ".."} for segment in value.split("/")):
        raise ValueError("resource must be a segment-aware relative resource prefix without dot segments")
    return value


class ResourceClaims(FrozenModel):
    reads: tuple[str, ...] = ()
    writes: tuple[str, ...] = ()
    exclusive: tuple[str, ...] = ()

    @field_validator("reads", "writes", "exclusive")
    @classmethod
    def _validate_prefixes(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_validate_resource_prefix(value) for value in values)


class CandidateFile(FrozenModel):
    path: str
    before_sha256: str | None
    after_sha256: str | None


class CandidateWriteSet(FrozenModel):
    baseline_tree_id: str
    candidate_tree_id: str
    files: tuple[CandidateFile, ...]


class ValidationContext(FrozenModel):
    invocation_id: str
    task_id: str
    graph_instance_id: str
    node_id: str
    resources: ResourceClaims


class ValidationResult(FrozenModel):
    accepted: bool
    reason: str | None = None

    @model_validator(mode="after")
    def _validate_reason(self) -> ValidationResult:
        if self.accepted and self.reason is not None:
            raise ValueError("reason is allowed only when validation is rejected")
        if not self.accepted and not self.reason:
            raise ValueError("a non-empty reason is required when validation is rejected")
        return self


class CommitValidator(Protocol):
    def validate(self, candidate: CandidateWriteSet, context: ValidationContext) -> ValidationResult: ...


@dataclass(frozen=True, slots=True)
class RegistryPorts:
    engine_api: str

    def __post_init__(self) -> None:
        _validate_engine_api(self.engine_api)


@dataclass(frozen=True, slots=True)
class PluginDependency:
    plugin_id: str
    version_specifier: str

    def __post_init__(self) -> None:
        _validate_contract_id(self.plugin_id, "dependency plugin id")
        _validate_specifier(self.version_specifier, "dependency version specifier")


@dataclass(frozen=True, slots=True)
class SchemaContribution:
    schema_id: str
    media_type: str
    content: bytes

    def __post_init__(self) -> None:
        _validate_contract_id(self.schema_id, "schema id")
        _validate_media_type(self.media_type, "schema media type")
        object.__setattr__(self, "content", bytes(self.content))


@dataclass(frozen=True, slots=True)
class ResourceContribution:
    resource_id: str
    media_type: str
    content: bytes

    def __post_init__(self) -> None:
        _validate_contract_id(self.resource_id, "resource id")
        _validate_media_type(self.media_type, "resource media type")
        object.__setattr__(self, "content", bytes(self.content))


class CapabilityBindingContribution(FrozenModel):
    capability_id: str
    target_capability_id: str
    data: FrozenJSONValue = None
    resource_ids: tuple[str, ...] = ()

    @field_validator("capability_id", "target_capability_id")
    @classmethod
    def _validate_capability_id(cls, value: str) -> str:
        try:
            return validate_qualified_id(value)
        except IdentifierError as error:
            raise ValueError(f"invalid capability id: {value!r}") from error

    @field_validator("resource_ids")
    @classmethod
    def _validate_resource_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_validate_contract_id(value, "binding resource id") for value in values)


@dataclass(frozen=True, slots=True)
class EffectRegistration:
    kind: str
    intent_schema_id: str
    receipt_schema_id: str
    handler: DurableEffectHandler
    policy: EffectPolicy

    def __post_init__(self) -> None:
        _validate_contract_id(self.kind, "effect kind")
        _validate_contract_id(self.intent_schema_id, "effect intent schema id")
        _validate_contract_id(self.receipt_schema_id, "effect receipt schema id")


@dataclass(frozen=True, slots=True)
class PluginContribution:
    task_handlers: Mapping[str, TaskHandler] = dataclass_field(default_factory=dict)
    commit_validators: Mapping[str, CommitValidator] = dataclass_field(default_factory=dict)
    schemas: tuple[SchemaContribution, ...] = ()
    resources: tuple[ResourceContribution, ...] = ()
    effects: tuple[EffectRegistration, ...] = ()
    bindings: tuple[CapabilityBindingContribution, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_handlers", _freeze_mapping(self.task_handlers, "task handler id"))
        object.__setattr__(
            self,
            "commit_validators",
            _freeze_mapping(self.commit_validators, "commit validator id"),
        )
        object.__setattr__(self, "schemas", tuple(self.schemas))
        object.__setattr__(self, "resources", tuple(self.resources))
        object.__setattr__(self, "effects", tuple(self.effects))
        object.__setattr__(self, "bindings", tuple(self.bindings))

    @classmethod
    def empty(cls) -> PluginContribution:
        return cls()


@dataclass(frozen=True, slots=True)
class PluginDescriptor:
    plugin_id: str
    plugin_version: str
    engine_api: str
    task_handlers: tuple[str, ...]
    commit_validators: tuple[str, ...]
    dependencies: tuple[PluginDependency, ...] = ()
    schemas: tuple[str, ...] = ()
    resources: tuple[str, ...] = ()
    effects: tuple[str, ...] = ()
    bindings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _validate_version(self.plugin_version, "plugin version")
        _validate_engine_api(self.engine_api)
        object.__setattr__(self, "dependencies", tuple(self.dependencies))
        object.__setattr__(self, "task_handlers", tuple(self.task_handlers))
        object.__setattr__(self, "commit_validators", tuple(self.commit_validators))
        object.__setattr__(self, "schemas", tuple(self.schemas))
        object.__setattr__(self, "resources", tuple(self.resources))
        object.__setattr__(self, "effects", tuple(self.effects))
        object.__setattr__(self, "bindings", tuple(self.bindings))


class PluginProvider(Protocol):
    def descriptor(self) -> PluginDescriptor: ...

    def contribute(self, ports: RegistryPorts) -> PluginContribution: ...


def _validate_contract_id(value: object, kind: str) -> str:
    if not isinstance(value, str):
        raise PluginContractError(f"invalid {kind}: {value!r}")
    try:
        return validate_qualified_id(value)
    except IdentifierError as error:
        raise PluginContractError(f"invalid {kind}: {value!r}") from error


def _validate_version(value: object, kind: str) -> str:
    if not isinstance(value, str):
        raise PluginContractError(f"invalid {kind}: {value!r}")
    try:
        Version(value)
    except InvalidVersion as error:
        raise PluginContractError(f"invalid {kind}: {value!r}") from error
    return value


def _validate_specifier(value: object, kind: str) -> str:
    if not isinstance(value, str):
        raise PluginContractError(f"invalid {kind}: {value!r}")
    try:
        SpecifierSet(value)
    except InvalidSpecifier as error:
        raise PluginContractError(f"invalid {kind}: {value!r}") from error
    return value


def _validate_engine_api(value: object) -> str:
    if not isinstance(value, str):
        raise PluginContractError(f"invalid engine API: {value!r}")
    try:
        Version(value)
    except InvalidVersion:
        return _validate_specifier(value, "engine API specifier")
    return value


def _validate_media_type(value: object, kind: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PluginContractError(f"invalid {kind}: {value!r}")
    return value


def _freeze_mapping(capabilities: Mapping[str, _Capability], kind: str) -> Mapping[str, _Capability]:
    if not isinstance(capabilities, Mapping):
        raise PluginContractError(f"{kind}s must be a mapping")
    copied = dict(capabilities)
    for capability_id in copied:
        _validate_contract_id(capability_id, kind)
    return MappingProxyType(dict(sorted(copied.items())))


def _contribution_ids(
    contribution: PluginContribution,
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    return (
        ("task handler", tuple(contribution.task_handlers)),
        ("commit validator", tuple(contribution.commit_validators)),
        ("schema", tuple(entry.schema_id for entry in contribution.schemas)),
        ("resource", tuple(entry.resource_id for entry in contribution.resources)),
        ("effect", tuple(entry.kind for entry in contribution.effects)),
        ("binding", tuple(entry.capability_id for entry in contribution.bindings)),
    )


def _validate_unique_contribution_ids(groups: tuple[tuple[str, tuple[str, ...]], ...]) -> None:
    seen: dict[str, str] = {}
    for kind, ids in groups:
        for contribution_id in ids:
            _validate_contract_id(contribution_id, f"{kind} id")
            previous_kind = seen.get(contribution_id)
            if previous_kind == kind:
                raise PluginContractError(f"duplicate {kind} id: {contribution_id}")
            if previous_kind is not None:
                raise PluginContractError(
                    f"cross-kind contribution id: {contribution_id} is both {previous_kind} and {kind}"
                )
            seen[contribution_id] = kind


def _validate_descriptor_ids(descriptor: PluginDescriptor) -> tuple[tuple[str, tuple[str, ...]], ...]:
    groups = (
        ("task handler", descriptor.task_handlers),
        ("commit validator", descriptor.commit_validators),
        ("schema", descriptor.schemas),
        ("resource", descriptor.resources),
        ("effect", descriptor.effects),
        ("binding", descriptor.bindings),
    )
    _validate_unique_contribution_ids(groups)
    return groups


def validate_contribution(descriptor: PluginDescriptor, contribution: PluginContribution) -> None:
    """Require an immutable contribution to exactly realize its descriptor."""

    _validate_contract_id(descriptor.plugin_id, "plugin id")
    _validate_version(descriptor.plugin_version, "plugin version")
    _validate_engine_api(descriptor.engine_api)
    descriptor_groups = _validate_descriptor_ids(descriptor)
    contribution_groups = _contribution_ids(contribution)
    _validate_unique_contribution_ids(contribution_groups)

    for (kind, declared), (_, contributed) in zip(descriptor_groups, contribution_groups, strict=True):
        if set(declared) != set(contributed):
            raise PluginContractError(f"{kind} declarations disagree with contribution")


__all__ = [
    "CapabilityBindingContribution",
    "CandidateFile",
    "CandidateWriteSet",
    "CommitValidator",
    "DurableEffectHandler",
    "EffectApplyResult",
    "EffectIntent",
    "EffectPolicy",
    "EffectReconcileResult",
    "EffectRegistration",
    "FailureKind",
    "FrozenModel",
    "PluginContribution",
    "PluginContractError",
    "PluginDependency",
    "PluginDescriptor",
    "PluginProvider",
    "RegistryPorts",
    "ResourceContribution",
    "ResourceClaims",
    "SchemaContribution",
    "TaskContext",
    "TaskFailure",
    "TaskHandler",
    "TaskOutcome",
    "TaskRequest",
    "TaskStatus",
    "ValidationContext",
    "ValidationResult",
    "validate_contribution",
]
