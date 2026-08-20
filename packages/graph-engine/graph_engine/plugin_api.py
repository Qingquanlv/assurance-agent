from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal, Protocol, TypeVar

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

from graph_engine import ENGINE_API_VERSION
from graph_engine.errors import GraphEngineError
from graph_engine.identifiers import IdentifierError, validate_qualified_id

if TYPE_CHECKING:
    from graph_engine.canonical import JSONValue
else:
    JSONValue = JsonValue


class CapabilityRegistryError(GraphEngineError):
    """Raised when plugin declarations cannot form an exact capability registry."""


FailureKind = Literal["transient", "timeout", "invalid_input", "invalid_output", "internal"]
TaskStatus = Literal["succeeded", "failed", "stopped"]

_FROZEN_MODEL_CONFIG = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
_Capability = TypeVar("_Capability")


class TaskFailure(BaseModel):
    model_config = _FROZEN_MODEL_CONFIG

    kind: FailureKind
    message: str


class TaskRequest(BaseModel):
    model_config = _FROZEN_MODEL_CONFIG

    invocation_id: str
    task_id: str
    graph_instance_id: str
    node_id: str
    capability_id: str
    attempt: int = Field(ge=1)
    input: JSONValue
    prior_failure: TaskFailure | None = None


class TaskOutcome(BaseModel):
    model_config = _FROZEN_MODEL_CONFIG

    status: TaskStatus
    output: JSONValue = None
    failure: TaskFailure | None = None
    stop_reason: str | None = None

    @model_validator(mode="after")
    def _validate_status_fields(self) -> TaskOutcome:
        if (self.failure is not None) != (self.status == "failed"):
            raise ValueError("failure is required exactly when status is failed")
        if self.status == "stopped":
            if not self.stop_reason:
                raise ValueError("a non-empty stop_reason is required when status is stopped")
        elif self.stop_reason is not None:
            raise ValueError("stop_reason is allowed only when status is stopped")
        return self

    @classmethod
    def succeeded(cls, output: JSONValue = None) -> TaskOutcome:
        return cls(status="succeeded", output=output)

    @classmethod
    def failed(cls, kind: FailureKind, message: str) -> TaskOutcome:
        return cls(status="failed", failure=TaskFailure(kind=kind, message=message))

    @classmethod
    def stopped(cls, reason: str, output: JSONValue = None) -> TaskOutcome:
        return cls(status="stopped", output=output, stop_reason=reason)


@dataclass(frozen=True, slots=True)
class TaskContext:
    workspace_root: Path
    heartbeat: Callable[[], None]


class TaskHandler(Protocol):
    async def __call__(self, request: TaskRequest, context: TaskContext) -> TaskOutcome: ...


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


class ResourceClaims(BaseModel):
    model_config = _FROZEN_MODEL_CONFIG

    reads: tuple[str, ...] = ()
    writes: tuple[str, ...] = ()
    exclusive: tuple[str, ...] = ()

    @field_validator("reads", "writes", "exclusive")
    @classmethod
    def _validate_prefixes(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_validate_resource_prefix(value) for value in values)


class CandidateFile(BaseModel):
    model_config = _FROZEN_MODEL_CONFIG

    path: str
    before_sha256: str | None
    after_sha256: str | None


class CandidateWriteSet(BaseModel):
    model_config = _FROZEN_MODEL_CONFIG

    baseline_tree_id: str
    candidate_tree_id: str
    files: tuple[CandidateFile, ...]


class ValidationContext(BaseModel):
    model_config = _FROZEN_MODEL_CONFIG

    invocation_id: str
    task_id: str
    graph_instance_id: str
    node_id: str
    resources: ResourceClaims


class ValidationResult(BaseModel):
    model_config = _FROZEN_MODEL_CONFIG

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
class EnginePorts:
    engine_api: str = ENGINE_API_VERSION


@dataclass(frozen=True, slots=True)
class PluginDescriptor:
    plugin_id: str
    plugin_version: str
    engine_api: str
    task_handlers: tuple[str, ...]
    commit_validators: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PluginRuntime:
    task_handlers: Mapping[str, TaskHandler]
    commit_validators: Mapping[str, CommitValidator]


class PluginProvider(Protocol):
    def descriptor(self) -> PluginDescriptor: ...

    def bind(self, ports: EnginePorts) -> PluginRuntime: ...


@dataclass(frozen=True, slots=True)
class CapabilityRegistry:
    task_handlers: Mapping[str, TaskHandler]
    commit_validators: Mapping[str, CommitValidator]

    def __post_init__(self) -> None:
        task_handlers = dict(self.task_handlers)
        commit_validators = dict(self.commit_validators)
        for capability_id in task_handlers:
            _validated_id(capability_id, "task handler id")
        for capability_id in commit_validators:
            _validated_id(capability_id, "commit validator id")
        overlap = task_handlers.keys() & commit_validators.keys()
        if overlap:
            capability_id = min(overlap)
            raise CapabilityRegistryError(f"capability cannot be both handler and validator: {capability_id}")

        object.__setattr__(self, "task_handlers", MappingProxyType(dict(sorted(task_handlers.items()))))
        object.__setattr__(
            self,
            "commit_validators",
            MappingProxyType(dict(sorted(commit_validators.items()))),
        )

    @classmethod
    def empty(cls) -> CapabilityRegistry:
        return cls(task_handlers={}, commit_validators={})


def _validated_id(value: object, kind: str) -> str:
    if not isinstance(value, str):
        raise CapabilityRegistryError(f"invalid {kind}: {value!r}")
    try:
        return validate_qualified_id(value)
    except IdentifierError as error:
        raise CapabilityRegistryError(f"invalid {kind}: {value!r}") from error


def _validate_declared_ids(ids: tuple[str, ...], kind: str) -> None:
    for capability_id in ids:
        _validated_id(capability_id, kind)
    if len(set(ids)) != len(ids):
        raise CapabilityRegistryError(f"duplicate declared {kind}")


def _validate_runtime_ids(ids: object, kind: str) -> tuple[str, ...]:
    if not isinstance(ids, Mapping):
        raise CapabilityRegistryError(f"bound {kind} must be a mapping")
    keys = tuple(ids)
    for capability_id in keys:
        if not isinstance(capability_id, str):
            raise CapabilityRegistryError(f"invalid bound {kind} id: {capability_id!r}")
        _validated_id(capability_id, f"bound {kind} id")
    return keys


def _snapshot_runtime_mapping(capabilities: Mapping[str, _Capability], kind: str) -> dict[str, _Capability]:
    if not isinstance(capabilities, Mapping):
        raise CapabilityRegistryError(f"bound {kind} must be a mapping")
    return dict(capabilities)


def _ensure_exact_binding(declared: tuple[str, ...], bound: tuple[str, ...], kind: str) -> None:
    if set(declared) != set(bound):
        raise CapabilityRegistryError(f"declared and bound {kind} differ")


def assemble_registry(providers: Sequence[PluginProvider]) -> CapabilityRegistry:
    plugin_ids: set[str] = set()
    capability_ids: set[str] = set()
    task_handlers: dict[str, TaskHandler] = {}
    commit_validators: dict[str, CommitValidator] = {}
    ports = EnginePorts()

    for provider in providers:
        descriptor = provider.descriptor()
        plugin_id = _validated_id(descriptor.plugin_id, "plugin id")
        if plugin_id in plugin_ids:
            raise CapabilityRegistryError(f"duplicate plugin id: {plugin_id}")
        plugin_ids.add(plugin_id)

        if descriptor.engine_api != ENGINE_API_VERSION:
            raise CapabilityRegistryError(
                f"plugin {plugin_id} requires engine API {descriptor.engine_api!r}; "
                f"expected {ENGINE_API_VERSION!r}"
            )

        _validate_declared_ids(descriptor.task_handlers, "task handler id")
        _validate_declared_ids(descriptor.commit_validators, "commit validator id")

        declared_capabilities = (*descriptor.task_handlers, *descriptor.commit_validators)
        for capability_id in declared_capabilities:
            if capability_id in capability_ids:
                kind = "task handler" if capability_id in descriptor.task_handlers else "commit validator"
                raise CapabilityRegistryError(f"duplicate {kind}: {capability_id}")
            capability_ids.add(capability_id)

        runtime = provider.bind(ports)
        runtime_task_handlers = _snapshot_runtime_mapping(runtime.task_handlers, "task handler")
        runtime_commit_validators = _snapshot_runtime_mapping(runtime.commit_validators, "commit validator")
        bound_task_ids = _validate_runtime_ids(runtime_task_handlers, "task handler")
        bound_validator_ids = _validate_runtime_ids(runtime_commit_validators, "commit validator")
        _ensure_exact_binding(descriptor.task_handlers, bound_task_ids, "task handlers")
        _ensure_exact_binding(descriptor.commit_validators, bound_validator_ids, "commit validators")

        task_handlers.update(runtime_task_handlers)
        commit_validators.update(runtime_commit_validators)

    return CapabilityRegistry(
        task_handlers=task_handlers,
        commit_validators=commit_validators,
    )


__all__ = [
    "CandidateFile",
    "CandidateWriteSet",
    "CapabilityRegistry",
    "CapabilityRegistryError",
    "CommitValidator",
    "EnginePorts",
    "FailureKind",
    "PluginDescriptor",
    "PluginProvider",
    "PluginRuntime",
    "ResourceClaims",
    "TaskContext",
    "TaskFailure",
    "TaskHandler",
    "TaskOutcome",
    "TaskRequest",
    "TaskStatus",
    "ValidationContext",
    "ValidationResult",
    "assemble_registry",
]
