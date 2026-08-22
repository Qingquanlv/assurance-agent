from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field as dataclass_field
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypeVar, cast, runtime_checkable

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import canonicalize_name
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

from graph_engine.canonical import canonical_digest
from graph_engine.errors import GraphEngineError
from graph_engine.frozen_json import FrozenJSONValue, freeze_json, thaw_json
from graph_engine.identifiers import IdentifierError, validate_qualified_id

if TYPE_CHECKING:
    from graph_engine.canonical import JSONValue
else:
    JSONValue = JsonValue


class PluginContractError(GraphEngineError):
    """Raised when a frozen Phase 2 plugin contribution violates its descriptor."""


class SecretHandleUnauthorized(GraphEngineError):
    """Raised when a secret handle is not authorized for the current host call."""


FailureKind = Literal[
    "transient",
    "timeout",
    "invalid_input",
    "invalid_output",
    "external_effect",
    "internal",
    "configuration",
]
TaskStatus = Literal["succeeded", "failed", "stopped"]

_FROZEN_MODEL_CONFIG = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_Capability = TypeVar("_Capability")
ActivityState = Literal["prepared", "dispatch_started", "bound", "terminal_observed"]


class FrozenModel(BaseModel):
    model_config = _FROZEN_MODEL_CONFIG


class InvocationMetadata(FrozenModel):
    invocation_id: str
    lock_digest: str = Field(pattern=_SHA256_PATTERN)
    composition_digest: str = Field(pattern=_SHA256_PATTERN)
    entrypoint: str = Field(min_length=1)


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
    resource_digests: FrozenJSONValue = Field(default_factory=dict)
    resources: ResourceClaims = Field(default_factory=lambda: ResourceClaims())
    invocation: InvocationMetadata
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
        return tuple(sorted(validated))

    @field_validator("resource_digests", mode="after")
    @classmethod
    def _freeze_resource_digests(cls, value: object) -> Any:
        frozen = freeze_json(value)
        if not isinstance(frozen, Mapping):
            raise ValueError("resource digests must be a mapping")
        return frozen

    @field_serializer("binding_data", "resource_digests")
    def _serialize_json_fields(self, value: object) -> Any:
        return thaw_json(value)

    @model_validator(mode="after")
    def _validate_resource_digest_keys(self) -> TaskRequest:
        digests = thaw_json(self.resource_digests)
        if not isinstance(digests, dict):
            raise ValueError("resource digests must be a mapping")
        if set(digests) != set(self.resource_ids):
            raise ValueError("resource digests must be keyed by the exact resource ids")
        for digest in digests.values():
            if (
                not isinstance(digest, str)
                or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
            ):
                raise ValueError("resource digest must be a lowercase SHA-256 hex value")
        return self


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


def _canonical_json_digest(value: object) -> str:
    return canonical_digest(cast("JSONValue", thaw_json(value)))


def _require_digest_pair(value: object, digest: str | None, label: str) -> None:
    if (value is None) != (digest is None):
        raise ValueError(f"{label} and {label} digest must be present together")
    if value is not None and digest != _canonical_json_digest(value):
        raise ValueError(f"{label} digest is not canonical")


def _require_terminal_observation(
    *,
    terminal: TaskOutcome | None,
    outcome_digest: str | None,
    candidate_tree_id: str | None,
    write_set_digest: str | None,
    required: bool,
) -> None:
    if not required:
        if (
            terminal is not None
            or outcome_digest is not None
            or candidate_tree_id is not None
            or write_set_digest is not None
        ):
            raise ValueError("terminal outcome is allowed only for terminal_observed")
        return
    if terminal is None or outcome_digest is None:
        raise ValueError("terminal activity requires a canonical outcome digest")
    if outcome_digest != _canonical_json_digest(terminal.model_dump(mode="json")):
        raise ValueError("terminal activity requires a canonical outcome digest")
    succeeded = terminal.status == "succeeded"
    if succeeded != (candidate_tree_id is not None) or succeeded != (write_set_digest is not None):
        raise ValueError(
            "candidate tree and write-set digests are required exactly for a succeeded terminal outcome"
        )


class AttemptWorkspaceIdentity(FrozenModel):
    attempt_directory_id: str
    baseline_tree_id: str = Field(pattern=_SHA256_PATTERN)
    attempt_identity_digest: str = Field(pattern=_SHA256_PATTERN)
    layout_schema_version: Literal["1"] = "1"

    @field_validator("attempt_directory_id")
    @classmethod
    def _reject_host_path(cls, value: str) -> str:
        windows_path = PureWindowsPath(value)
        if (
            not value
            or "/" in value
            or "\\" in value
            or value in {".", ".."}
            or windows_path.is_absolute()
            or bool(windows_path.drive)
        ):
            raise ValueError("attempt directory id must not contain a host path")
        return value


class TaskActivitySnapshot(FrozenModel):
    activity_id: str = Field(min_length=1)
    request_digest: str = Field(pattern=_SHA256_PATTERN)
    workspace_identity: AttemptWorkspaceIdentity
    state: ActivityState
    reference: JSONValue | None = None
    reference_digest: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    dispatch_fingerprint: JSONValue | None = None
    dispatch_fingerprint_digest: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    cancel_requested: bool = False
    cancel_reason: str | None = None
    terminal: TaskOutcome | None = None
    outcome_digest: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    terminal_proof_digest: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    candidate_tree_id: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    write_set_digest: str | None = Field(default=None, pattern=_SHA256_PATTERN)

    @field_validator("reference", "dispatch_fingerprint", mode="after")
    @classmethod
    def _freeze_json_fields(cls, value: JSONValue) -> Any:
        return freeze_json(value)

    @field_serializer("reference", "dispatch_fingerprint")
    def _serialize_json_fields(self, value: object) -> Any:
        return thaw_json(value)

    @model_validator(mode="after")
    def _validate_state_fields(self) -> TaskActivitySnapshot:
        _require_digest_pair(self.reference, self.reference_digest, "reference")
        _require_digest_pair(
            self.dispatch_fingerprint,
            self.dispatch_fingerprint_digest,
            "dispatch fingerprint",
        )
        if self.cancel_requested:
            if not self.cancel_reason:
                raise ValueError("a non-empty cancel_reason is required when cancel is requested")
        elif self.cancel_reason is not None:
            raise ValueError("cancel_reason is allowed only when cancel is requested")
        if self.state in {"prepared", "dispatch_started"} and self.reference is not None:
            raise ValueError("reference is forbidden on states that do not bind one")
        if self.state == "bound" and self.reference is None:
            raise ValueError("bound activity requires a reference")
        if self.state == "prepared":
            if self.dispatch_fingerprint is not None:
                raise ValueError("dispatch fingerprint is forbidden before dispatch")
        elif self.state in {"dispatch_started", "bound"} and self.dispatch_fingerprint is None:
            raise ValueError("dispatch fingerprint is required after dispatch starts")
        terminal_required = self.state == "terminal_observed"
        _require_terminal_observation(
            terminal=self.terminal,
            outcome_digest=self.outcome_digest,
            candidate_tree_id=self.candidate_tree_id,
            write_set_digest=self.write_set_digest,
            required=terminal_required,
        )
        if not terminal_required:
            if self.terminal_proof_digest is not None:
                raise ValueError("terminal_proof_digest is allowed only for terminal_observed")
            return self
        if self.terminal is None:
            raise ValueError("terminal activity requires a canonical outcome digest")
        dispatched = self.dispatch_fingerprint is not None
        bound = self.reference is not None
        if self.terminal.status == "succeeded":
            if not bound:
                raise ValueError("succeeded terminal activity requires a bound reference")
            if not dispatched:
                raise ValueError("succeeded terminal activity requires a dispatch fingerprint")
            if self.terminal_proof_digest is not None:
                raise ValueError("terminal_proof_digest is forbidden for bound terminal activity")
        elif dispatched and not bound:
            if self.terminal_proof_digest is None:
                raise ValueError("unbound terminal after dispatch_started requires terminal_proof_digest")
        elif self.terminal_proof_digest is not None:
            raise ValueError(
                "terminal_proof_digest is allowed only for unbound terminal after dispatch_started"
            )
        if not dispatched and bound:
            raise ValueError("reference is forbidden on states that do not bind one")
        return self


class TaskActivityReconcileResult(FrozenModel):
    status: Literal["not_dispatched", "running", "terminal", "absent", "indeterminate"]
    reference: JSONValue | None = None
    outcome: TaskOutcome | None = None
    proof: JSONValue | None = None
    reason: str | None = None

    @field_validator("reference", "proof", mode="after")
    @classmethod
    def _freeze_json_fields(cls, value: JSONValue) -> Any:
        return freeze_json(value)

    @field_serializer("reference", "proof")
    def _serialize_json_fields(self, value: object) -> Any:
        return thaw_json(value)

    @model_validator(mode="after")
    def _validate_status_fields(self) -> TaskActivityReconcileResult:
        if (self.outcome is not None) != (self.status == "terminal"):
            raise ValueError("outcome is required exactly when status is terminal")
        if (self.proof is not None) != (self.status == "absent"):
            raise ValueError("canonical proof is required exactly when status is absent")
        if self.status in {"not_dispatched", "absent"} and self.reference is not None:
            raise ValueError("reference is forbidden on states that do not bind one")
        if self.status == "indeterminate":
            if not self.reason:
                raise ValueError("a non-empty reason is required when status is indeterminate")
        elif self.reason is not None:
            raise ValueError("reason is allowed only when status is indeterminate")
        return self


class TaskActivityCancelResult(FrozenModel):
    status: Literal["acknowledged", "terminal", "indeterminate"]
    outcome: TaskOutcome | None = None
    reason: str | None = None

    @model_validator(mode="after")
    def _validate_status_fields(self) -> TaskActivityCancelResult:
        if (self.outcome is not None) != (self.status == "terminal"):
            raise ValueError("outcome is required exactly when status is terminal")
        if self.status == "indeterminate":
            if not self.reason:
                raise ValueError("a non-empty reason is required when status is indeterminate")
        elif self.reason is not None:
            raise ValueError("reason is allowed only when status is indeterminate")
        return self


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


@runtime_checkable
class SecretPort(Protocol):
    def resolve(self, handle: str) -> bytes: ...


@runtime_checkable
class TaskActivityPort(Protocol):
    @property
    def snapshot(self) -> TaskActivitySnapshot: ...

    def mark_dispatch_started(self, fingerprint: JSONValue) -> TaskActivitySnapshot: ...

    def bind(self, reference: JSONValue) -> TaskActivitySnapshot: ...


@dataclass(frozen=True, slots=True)
class TaskContext:
    workspace_root: Path
    heartbeat: Callable[[], None]
    cancel_requested: Callable[[], bool]
    invocation: InvocationMetadata
    activity: TaskActivityPort | None = None
    secrets: SecretPort | None = None

    def effect(self, kind: str, payload: JSONValue) -> EffectIntent:
        return EffectIntent(kind=kind, payload=payload)


class TaskHandler(Protocol):
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome: ...


@runtime_checkable
class RecoverableTaskHandler(TaskHandler, Protocol):
    async def reconcile(
        self,
        request: TaskRequest,
        context: TaskContext,
        activity: TaskActivitySnapshot,
    ) -> TaskActivityReconcileResult: ...

    async def cancel(
        self,
        request: TaskRequest,
        context: TaskContext,
        activity: TaskActivitySnapshot,
    ) -> TaskActivityCancelResult: ...


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
    secret_handles: tuple[str, ...] = ()

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

    @field_validator("secret_handles")
    @classmethod
    def _validate_secret_handles(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        validated = tuple(_validate_contract_id(value, "binding secret handle") for value in values)
        if len(validated) != len(set(validated)):
            raise ValueError("binding secret handles must be unique")
        return tuple(sorted(validated))


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


class ProviderSource(FrozenModel):
    distribution: str
    version: str
    entrypoint_group: Literal["graph_engine.products", "graph_engine.plugins"]
    entrypoint_name: str
    entrypoint_value: str
    declaration_path: str
    import_roots: tuple[str, ...]

    @field_validator("distribution")
    @classmethod
    def _normalize_distribution(cls, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("distribution must be non-empty text")
        normalized = canonicalize_name(value)
        if not normalized:
            raise ValueError("distribution must be non-empty text")
        return normalized

    @field_validator("version")
    @classmethod
    def _normalize_version(cls, value: str) -> str:
        try:
            return str(Version(value))
        except (InvalidVersion, TypeError) as error:
            raise ValueError(f"invalid provider source version: {value!r}") from error

    @field_validator("entrypoint_name", "entrypoint_value")
    @classmethod
    def _validate_entrypoint_coordinate(cls, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("provider entrypoint coordinate must be non-empty text")
        return value

    @field_validator("declaration_path")
    @classmethod
    def _validate_declaration_path(cls, value: str) -> str:
        if not isinstance(value, str) or not value or "\\" in value:
            raise ValueError("declaration path must be a canonical relative POSIX path")
        path = PurePosixPath(value)
        if (
            path.is_absolute()
            or path.as_posix() != value
            or any(part in ("", ".", "..") for part in path.parts)
        ):
            raise ValueError("declaration path must be a canonical relative POSIX path")
        return value

    @field_validator("import_roots")
    @classmethod
    def _validate_import_roots(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return validate_provider_import_roots(value)


def validate_provider_import_roots(value: tuple[str, ...]) -> tuple[str, ...]:
    if not value:
        raise ValueError("provider import roots must be a non-empty tuple")
    for import_root in value:
        if not isinstance(import_root, str) or "\\" in import_root:
            raise ValueError("provider import root must be a canonical relative POSIX path")
        if import_root:
            path = PurePosixPath(import_root)
            if (
                path.is_absolute()
                or path.as_posix() != import_root
                or any(part in ("", ".", "..") for part in path.parts)
            ):
                raise ValueError("provider import root must be a canonical relative POSIX path")
    if tuple(sorted(value)) != value or len(value) != len(set(value)):
        raise ValueError("provider import roots must have unique canonical order")
    return value


class PluginDescriptor(FrozenModel):
    schema_version: Literal["1"]
    source: ProviderSource | None
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

    @field_validator("plugin_id")
    @classmethod
    def _validate_plugin_id(cls, value: str) -> str:
        return _validate_contract_id(value, "plugin id")

    @field_validator("plugin_version")
    @classmethod
    def _normalize_plugin_version(cls, value: str) -> str:
        return str(Version(_validate_version(value, "plugin version")))

    @field_validator("engine_api")
    @classmethod
    def _validate_descriptor_engine_api(cls, value: str) -> str:
        return _validate_engine_api(value)

    @model_validator(mode="after")
    def _validate_source_expectation(self) -> PluginDescriptor:
        if self.source is None:
            return self
        if self.source.entrypoint_group != "graph_engine.plugins":
            raise ValueError("plugin source must use graph_engine.plugins")
        if Version(self.source.version) != Version(self.plugin_version):
            raise ValueError("source version must equal plugin version")
        return self


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
    "ActivityState",
    "AttemptWorkspaceIdentity",
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
    "InvocationMetadata",
    "PluginContribution",
    "PluginContractError",
    "PluginDependency",
    "PluginDescriptor",
    "PluginProvider",
    "ProviderSource",
    "RecoverableTaskHandler",
    "RegistryPorts",
    "ResourceContribution",
    "ResourceClaims",
    "SchemaContribution",
    "SecretHandleUnauthorized",
    "SecretPort",
    "TaskActivityCancelResult",
    "TaskActivityPort",
    "TaskActivityReconcileResult",
    "TaskActivitySnapshot",
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
