from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field as dataclass_field
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import stat
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
    SerializerFunctionWrapHandler,
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
    from graph_engine.attempts.keys import AttemptKey
    from graph_engine.attempts.resolutions import PermanentTaskFailure, RejectedTaskResult
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


class AttemptContractRef(FrozenModel):
    """Data-only Attempt contract identity authenticated for one owner."""

    contract_id: str
    digest: str = Field(pattern=_SHA256_PATTERN)

    @field_validator("contract_id")
    @classmethod
    def _validate_contract_id_field(cls, value: str) -> str:
        return _validate_contract_id(value, "attempt contract id")


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
    staged_write_set_digest: str | None,
    promotion_receipt_digest: str | None,
    required: bool,
) -> None:
    if not required:
        if (
            terminal is not None
            or outcome_digest is not None
            or staged_write_set_digest is not None
            or promotion_receipt_digest is not None
        ):
            raise ValueError("terminal outcome is allowed only for terminal_observed")
        return
    if terminal is None or outcome_digest is None:
        raise ValueError("terminal activity requires a canonical outcome digest")
    if outcome_digest != _canonical_json_digest(terminal.model_dump(mode="json")):
        raise ValueError("terminal activity requires a canonical outcome digest")
    if staged_write_set_digest is None:
        raise ValueError("terminal activity requires a staged write-set digest")
    if terminal.status != "succeeded" and promotion_receipt_digest is not None:
        raise ValueError("only successful terminal activity may carry a promotion receipt")


class TaskActivitySnapshot(FrozenModel):
    activity_id: str = Field(min_length=1)
    request_digest: str = Field(pattern=_SHA256_PATTERN)
    workspace_identity: TaskWorkspaceIdentity
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
    staged_write_set_digest: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    promotion_receipt_digest: str | None = Field(default=None, pattern=_SHA256_PATTERN)

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
            staged_write_set_digest=self.staged_write_set_digest,
            promotion_receipt_digest=self.promotion_receipt_digest,
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
    project_root: Path
    write_root: Path
    workspace_identity: TaskWorkspaceIdentity
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


_RESOURCE_PARAMETER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_RESOURCE_PLACEHOLDER_PATTERN = re.compile(r"^\{([A-Za-z_][A-Za-z0-9_]*)\}$")


def _validate_json_pointer(value: str) -> str:
    if not value.startswith("/") or value == "/":
        raise ValueError("resource parameter projection must be a non-root JSON pointer")
    for segment in value[1:].split("/"):
        if not segment:
            raise ValueError("resource parameter JSON pointer must not contain empty segments")
        index = 0
        while index < len(segment):
            if segment[index] != "~":
                index += 1
                continue
            if index + 1 >= len(segment) or segment[index + 1] not in {"0", "1"}:
                raise ValueError("resource parameter JSON pointer contains an invalid escape")
            index += 2
    return value


def _template_parameter_names(value: str) -> set[str]:
    windows_path = PureWindowsPath(value)
    if (
        not value
        or "\\" in value
        or value.startswith("/")
        or windows_path.is_absolute()
        or bool(windows_path.drive)
    ):
        raise ValueError("resource template must be a relative resource prefix")
    names: set[str] = set()
    for segment in value.split("/"):
        if segment in {"", ".", ".."}:
            raise ValueError("resource template must not contain empty or dot segments")
        placeholder = _RESOURCE_PLACEHOLDER_PATTERN.fullmatch(segment)
        if placeholder is not None:
            names.add(placeholder.group(1))
        elif "{" in segment or "}" in segment:
            raise ValueError("resource template placeholders must occupy a complete path component")
    return names


_NON_PORTABLE_RESOURCE_CHARACTERS = frozenset('<>:"|?*')
_NON_PORTABLE_RESOURCE_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)


def _validate_resource_parameter_component(value: str) -> str:
    basename = value.split(".", 1)[0].upper()
    if (
        not value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or value[-1] in {" ", "."}
        or basename in _NON_PORTABLE_RESOURCE_NAMES
        or any(character in _NON_PORTABLE_RESOURCE_CHARACTERS for character in value)
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("resource parameter resolved to an unsafe non-portable path component")
    return value


class ResourceClaimTemplate(FrozenModel):
    """Closed, business-neutral resource prefixes resolved from final task input."""

    parameters: Mapping[str, str]
    reads: tuple[str, ...] = ()
    writes: tuple[str, ...] = ()
    exclusive: tuple[str, ...] = ()

    @field_validator("parameters")
    @classmethod
    def _validate_parameters(cls, values: Mapping[str, str]) -> Mapping[str, str]:
        if not values:
            raise ValueError("resource template parameters must not be empty")
        normalized: dict[str, str] = {}
        for name, pointer in values.items():
            if _RESOURCE_PARAMETER_PATTERN.fullmatch(name) is None:
                raise ValueError(f"invalid resource template parameter: {name!r}")
            normalized[name] = _validate_json_pointer(pointer)
        return MappingProxyType(dict(sorted(normalized.items())))

    @field_serializer("parameters")
    def _serialize_parameters(self, values: Mapping[str, str]) -> dict[str, str]:
        return dict(values)

    @model_validator(mode="after")
    def _validate_closed_template(self) -> ResourceClaimTemplate:
        referenced: set[str] = set()
        for value in (*self.reads, *self.writes, *self.exclusive):
            referenced.update(_template_parameter_names(value))
        declared = set(self.parameters)
        unknown = referenced - declared
        unused = declared - referenced
        if unknown:
            raise ValueError(f"unknown resource template parameter: {sorted(unknown)[0]}")
        if unused:
            raise ValueError(f"unused resource template parameter: {sorted(unused)[0]}")
        return self

    def resolve(self, task_input: JSONValue) -> ResourceClaims:
        values = {
            name: _resolve_resource_pointer(task_input, pointer) for name, pointer in self.parameters.items()
        }

        def render(template: str) -> str:
            segments: list[str] = []
            for segment in template.split("/"):
                placeholder = _RESOURCE_PLACEHOLDER_PATTERN.fullmatch(segment)
                rendered = values[placeholder.group(1)] if placeholder is not None else segment
                segments.append(_validate_resource_parameter_component(rendered))
            return "/".join(segments)

        return ResourceClaims(
            reads=tuple(render(value) for value in self.reads),
            writes=tuple(render(value) for value in self.writes),
            exclusive=tuple(render(value) for value in self.exclusive),
        )


def _resolve_resource_pointer(value: JSONValue, pointer: str) -> str:
    current: object = value
    for raw_segment in pointer[1:].split("/"):
        segment = raw_segment.replace("~1", "/").replace("~0", "~")
        if isinstance(current, Mapping):
            if segment not in current:
                raise ValueError(f"resource parameter projection is missing: {pointer}")
            current = current[segment]
        elif isinstance(current, (list, tuple)):
            if not segment.isdigit():
                raise ValueError(f"resource parameter projection is not an array index: {pointer}")
            index = int(segment)
            if index >= len(current):
                raise ValueError(f"resource parameter projection is missing: {pointer}")
            current = current[index]
        else:
            raise ValueError(f"resource parameter projection traverses a scalar: {pointer}")
    if not isinstance(current, str):
        raise ValueError("resource parameter projection must resolve to a string")
    return current


def _validate_task_workspace_path(value: str) -> str:
    """Validate the logical, project-relative paths used by task workspaces."""

    return _validate_resource_prefix(value)


class StagedFile(FrozenModel):
    """One regular file authenticated by a task-workspace seal."""

    path: str
    before_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    after_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    before_mode: int | None = Field(default=None, ge=0, le=0o7777)
    after_mode: int | None = Field(default=None, ge=0, le=0o7777)

    @field_validator("path")
    @classmethod
    def _validate_path(cls, value: str) -> str:
        return _validate_task_workspace_path(value)

    @model_validator(mode="after")
    def _validate_digest_mode_pairs(self) -> StagedFile:
        if (self.before_sha256 is None) != (self.before_mode is None):
            raise ValueError("staged file before digest and mode must be present together")
        if (self.after_sha256 is None) != (self.after_mode is None):
            raise ValueError("staged file after digest and mode must be present together")
        return self


class TaskWorkspaceIdentity(FrozenModel):
    """Portable identity for one empty per-task staging root.

    Host paths are represented only by digests; ``TaskWorkspaceBinding`` keeps
    the process-local paths needed to execute a task.
    """

    task_id: str = Field(min_length=1)
    attempt: int = Field(ge=1)
    attempt_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    output_paths: tuple[str, ...]
    baseline_files: tuple[StagedFile, ...] = ()
    project_digest: str = Field(pattern=_SHA256_PATTERN)
    write_root_digest: str = Field(pattern=_SHA256_PATTERN)
    identity_digest: str = Field(pattern=_SHA256_PATTERN)
    layout_schema_version: Literal["1"] = "1"

    @field_validator("output_paths")
    @classmethod
    def _validate_output_paths(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(_validate_task_workspace_path(value) for value in values)
        if len(set(normalized)) != len(normalized):
            raise ValueError("output paths must be a tuple of unique paths")
        return normalized

    @field_validator("task_id")
    @classmethod
    def _reject_absolute_task_id(cls, value: str) -> str:
        windows_path = PureWindowsPath(value)
        if value.startswith("/") or windows_path.is_absolute() or bool(windows_path.drive):
            raise ValueError("task id must not contain an absolute host path")
        return value

    @model_validator(mode="after")
    def _validate_identity(self) -> TaskWorkspaceIdentity:
        paths = tuple(file.path for file in self.baseline_files)
        if paths != tuple(sorted(paths)) or len(paths) != len(set(paths)):
            raise ValueError("baseline files must have unique canonical order")
        for file in self.baseline_files:
            if (
                file.before_sha256 is None
                or file.before_mode is None
                or file.after_sha256 is not None
                or file.after_mode is not None
            ):
                raise ValueError("baseline files must contain only a before digest and mode")
            if not any(
                file.path == claim or file.path.startswith(f"{claim}/") for claim in self.output_paths
            ):
                raise ValueError("baseline file is outside the declared output paths")
        expected = canonical_digest(self.model_dump(mode="json", exclude={"identity_digest"}))
        if self.identity_digest != expected:
            raise ValueError("task workspace identity digest is not canonical")
        return self


class DirectoryIdentity(FrozenModel):
    """Path-confidential stable identity for one canonical directory entry."""

    path_digest: str = Field(pattern=_SHA256_PATTERN)
    device: int = Field(ge=0)
    inode: int = Field(ge=0)
    identity_digest: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _validate_identity(self) -> DirectoryIdentity:
        expected = canonical_digest(self.model_dump(mode="json", exclude={"identity_digest"}))
        if self.identity_digest != expected:
            raise ValueError("directory identity digest is not canonical")
        return self

    @classmethod
    def capture(cls, path: Path, *, descriptor: int | None = None) -> DirectoryIdentity:
        canonical = _canonical_binding_directory(path, "directory")
        owned = descriptor is None
        opened_descriptor = descriptor
        try:
            before = os.stat(canonical, follow_symlinks=False)
            if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
                raise ValueError("directory identity requires a real directory")
            if opened_descriptor is None:
                opened_descriptor = os.open(
                    canonical,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                )
            opened = os.fstat(opened_descriptor)
            after = os.stat(canonical, follow_symlinks=False)
            identities = {
                (before.st_dev, before.st_ino),
                (opened.st_dev, opened.st_ino),
                (after.st_dev, after.st_ino),
            }
            if not stat.S_ISDIR(opened.st_mode) or not stat.S_ISDIR(after.st_mode) or len(identities) != 1:
                raise ValueError("directory entry changed while authenticating")
            payload = {
                "path_digest": _binding_path_digest(canonical),
                "device": opened.st_dev,
                "inode": opened.st_ino,
            }
            return cls(**payload, identity_digest=canonical_digest(payload))
        except OSError as error:
            raise ValueError("directory identity is unavailable") from error
        finally:
            if owned and opened_descriptor is not None:
                os.close(opened_descriptor)


@dataclass(frozen=True, slots=True)
class TaskWorkspaceBinding:
    """Local process binding for a portable task-workspace identity."""

    identity: TaskWorkspaceIdentity
    project_root: Path
    write_root: Path
    project_root_identity: DirectoryIdentity
    write_root_identity: DirectoryIdentity


def _canonical_binding_directory(value: Path, label: str) -> Path:
    supplied = Path(value)
    if not supplied.is_absolute():
        raise ValueError(f"{label} must be an absolute canonical directory")
    try:
        resolved = supplied.resolve(strict=True)
    except OSError as error:
        raise ValueError(f"{label} must exist") from error
    if supplied != resolved or not resolved.is_dir():
        raise ValueError(f"{label} must be an absolute canonical directory")
    return resolved


def _binding_path_digest(path: Path) -> str:
    return canonical_digest({"path": str(path)})


@dataclass(frozen=True, slots=True)
class InvocationWorkspaceBinding:
    """Process-local canonical roots; only their digests enter invocation identity."""

    project_root: Path
    attempts_root: Path
    receipts_root: Path
    _project_root_identity: DirectoryIdentity = dataclass_field(init=False, repr=False, compare=False)
    _attempts_root_identity: DirectoryIdentity = dataclass_field(init=False, repr=False, compare=False)
    _receipts_root_identity: DirectoryIdentity = dataclass_field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "project_root", _canonical_binding_directory(self.project_root, "project root")
        )
        object.__setattr__(
            self, "attempts_root", _canonical_binding_directory(self.attempts_root, "attempts root")
        )
        object.__setattr__(
            self, "receipts_root", _canonical_binding_directory(self.receipts_root, "receipts root")
        )
        object.__setattr__(
            self,
            "_project_root_identity",
            DirectoryIdentity.capture(self.project_root),
        )
        object.__setattr__(
            self,
            "_attempts_root_identity",
            DirectoryIdentity.capture(self.attempts_root),
        )
        object.__setattr__(
            self,
            "_receipts_root_identity",
            DirectoryIdentity.capture(self.receipts_root),
        )

    @property
    def project_root_identity(self) -> DirectoryIdentity:
        return self._project_root_identity

    @property
    def attempts_root_identity(self) -> DirectoryIdentity:
        return self._attempts_root_identity

    @property
    def receipts_root_identity(self) -> DirectoryIdentity:
        return self._receipts_root_identity

    @property
    def project_root_digest(self) -> str:
        return self.project_root_identity.identity_digest

    @property
    def attempts_root_digest(self) -> str:
        return self.attempts_root_identity.identity_digest

    @property
    def receipts_root_digest(self) -> str:
        return self.receipts_root_identity.identity_digest

    @property
    def identity_digest(self) -> str:
        return canonical_digest(
            {
                "project_root_digest": self.project_root_digest,
                "attempts_root_digest": self.attempts_root_digest,
                "receipts_root_digest": self.receipts_root_digest,
            }
        )


class StagedWriteSet(FrozenModel):
    identity_digest: str = Field(pattern=_SHA256_PATTERN)
    files: tuple[StagedFile, ...]
    staged_digest: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _validate_write_set(self) -> StagedWriteSet:
        paths = tuple(file.path for file in self.files)
        if paths != tuple(sorted(paths)) or len(paths) != len(set(paths)):
            raise ValueError("staged files must have unique canonical order")
        if any(file.after_sha256 is None or file.after_mode is None for file in self.files):
            raise ValueError("staged files must contain an after digest and mode")
        expected = canonical_digest(
            {
                "identity_digest": self.identity_digest,
                "files": [file.model_dump(mode="json") for file in self.files],
            }
        )
        if self.staged_digest != expected:
            raise ValueError("staged write-set digest is not canonical")
        return self


class PromotionReceipt(FrozenModel):
    identity_digest: str = Field(pattern=_SHA256_PATTERN)
    staged_digest: str = Field(pattern=_SHA256_PATTERN)
    receipt_digest: str = Field(pattern=_SHA256_PATTERN)
    layout_schema_version: Literal["1"] = "1"

    @model_validator(mode="after")
    def _validate_receipt(self) -> PromotionReceipt:
        expected = canonical_digest(self.model_dump(mode="json", exclude={"receipt_digest"}))
        if self.receipt_digest != expected:
            raise ValueError("promotion receipt digest is not canonical")
        return self

    @property
    def sealed_digest(self) -> str:
        return self.staged_digest


class CandidateFile(FrozenModel):
    path: str
    before_sha256: str | None
    after_sha256: str | None


class CandidateWriteSet(FrozenModel):
    baseline_tree_id: str
    candidate_tree_id: str
    files: tuple[CandidateFile, ...]


@dataclass(frozen=True, slots=True)
class SealedFile:
    path: str
    before_sha256: str | None
    before_mode: int | None
    after_sha256: str
    after_mode: int
    content: bytes

    def __post_init__(self) -> None:
        object.__setattr__(self, "content", bytes(self.content))


@dataclass(frozen=True, slots=True)
class SealedWriteSet:
    files: tuple[SealedFile, ...]
    sealed_digest: str


@dataclass(frozen=True, slots=True)
class PreparedWorkspaceRef:
    identity: TaskWorkspaceIdentity
    sealed: SealedWriteSet
    prepared_digest: str


class WorkspaceProvider(Protocol):
    async def open_or_create(
        self, attempt_key: AttemptKey, claims: ResourceClaims
    ) -> TaskWorkspaceBinding: ...
    async def seal(self, binding: TaskWorkspaceBinding) -> SealedWriteSet: ...
    async def prepare(
        self, binding: TaskWorkspaceBinding, sealed: SealedWriteSet
    ) -> PreparedWorkspaceRef: ...
    async def promote(self, prepared: PreparedWorkspaceRef) -> PromotionReceipt: ...
    async def recover_promotion(self, prepared: PreparedWorkspaceRef) -> PromotionReceipt: ...


class ValidationContext(FrozenModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        allow_inf_nan=False,
        arbitrary_types_allowed=True,
    )

    invocation_id: str
    task_id: str
    graph_instance_id: str
    node_id: str
    resources: ResourceClaims
    task_input: JSONValue = None
    task_output: JSONValue = None
    evidence_refs: tuple[str, ...] = ()
    write_set: SealedWriteSet | None = None

    @field_validator("task_input", "task_output", mode="after")
    @classmethod
    def _freeze_semantic_json(cls, value: JSONValue) -> Any:
        return freeze_json(value)

    @field_serializer("task_input", "task_output")
    def _serialize_semantic_json(self, value: object) -> Any:
        return thaw_json(value)


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


class PathBearingFile(Protocol):
    @property
    def path(self) -> str: ...

    @property
    def before_sha256(self) -> str | None: ...

    @property
    def after_sha256(self) -> str | None: ...


class PathWriteSet(Protocol):
    @property
    def files(self) -> Sequence[PathBearingFile]: ...


class CommitValidator(Protocol):
    def validate(self, staged: PathWriteSet, context: ValidationContext) -> ValidationResult: ...


def run_validators(
    validator_ids: tuple[str, ...],
    registry: Mapping[str, CommitValidator],
    write_set: PathWriteSet,
    context: ValidationContext,
) -> RejectedTaskResult | PermanentTaskFailure | None:
    """Run contract validators in declared order. An empty tuple is an explicit no-op."""

    from graph_engine.attempts.resolutions import PermanentTaskFailure, RejectedTaskResult

    if validator_ids == ():
        return None
    for validator_id in validator_ids:
        try:
            validator = registry[validator_id]
        except KeyError:
            return PermanentTaskFailure(
                kind="configuration",
                message=f"validator {validator_id} is not registered",
            )
        try:
            result = validator.validate(write_set, context)
        except Exception as error:
            return PermanentTaskFailure(
                kind="internal",
                message=f"validator {validator_id} failed: {error}",
            )
        if not isinstance(result, ValidationResult):
            return PermanentTaskFailure(
                kind="internal",
                message=(
                    f"validator {validator_id} returned {type(result).__name__}, expected ValidationResult"
                ),
            )
        if not result.accepted:
            return RejectedTaskResult(reason=result.reason or validator_id)
    return None


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
    contract_id: str | None = None

    @field_validator("capability_id", "target_capability_id")
    @classmethod
    def _validate_capability_id(cls, value: str) -> str:
        try:
            return validate_qualified_id(value)
        except IdentifierError as error:
            raise ValueError(f"invalid capability id: {value!r}") from error

    @field_validator("contract_id")
    @classmethod
    def _validate_contract_id_field(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            return validate_qualified_id(value)
        except IdentifierError as error:
            raise ValueError(f"invalid contract id: {value!r}") from error

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
    attempt_contracts: tuple[AttemptContractRef, ...] = ()

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
        object.__setattr__(self, "attempt_contracts", _freeze_attempt_contracts(self.attempt_contracts))

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
    attempt_contracts: tuple[AttemptContractRef, ...] = ()

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

    @field_validator("attempt_contracts")
    @classmethod
    def _validate_attempt_contracts(
        cls, values: tuple[AttemptContractRef, ...]
    ) -> tuple[AttemptContractRef, ...]:
        contracts = tuple(values)
        contract_ids = tuple(item.contract_id for item in contracts)
        if contract_ids != tuple(sorted(contract_ids)) or len(contract_ids) != len(set(contract_ids)):
            raise ValueError("attempt contracts require unique canonical order")
        return contracts

    @model_validator(mode="after")
    def _validate_source_expectation(self) -> PluginDescriptor:
        if self.source is None:
            return self
        if self.source.entrypoint_group != "graph_engine.plugins":
            raise ValueError("plugin source must use graph_engine.plugins")
        if Version(self.source.version) != Version(self.plugin_version):
            raise ValueError("source version must equal plugin version")
        return self

    @model_serializer(mode="wrap")
    def _omit_empty_attempt_contracts(self, serializer: SerializerFunctionWrapHandler) -> object:
        data = serializer(self)
        if isinstance(data, dict) and not data.get("attempt_contracts"):
            data.pop("attempt_contracts", None)
        return data


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


def _freeze_attempt_contracts(values: object) -> tuple[AttemptContractRef, ...]:
    if not isinstance(values, tuple | list):
        raise PluginContractError("attempt contracts must be a tuple")
    contracts = tuple(
        item if isinstance(item, AttemptContractRef) else AttemptContractRef.model_validate(item)
        for item in values
    )
    contract_ids = tuple(item.contract_id for item in contracts)
    if contract_ids != tuple(sorted(contract_ids)) or len(contract_ids) != len(set(contract_ids)):
        raise PluginContractError("attempt contracts require unique canonical order")
    return contracts


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
        ("attempt contract", tuple(item.contract_id for item in contribution.attempt_contracts)),
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
        ("attempt contract", tuple(item.contract_id for item in descriptor.attempt_contracts)),
    )
    _validate_unique_contribution_ids(groups)
    return groups


def _attempt_contract_pairs(
    values: tuple[AttemptContractRef, ...],
) -> tuple[tuple[str, str], ...]:
    return tuple((item.contract_id, item.digest) for item in values)


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
    if _attempt_contract_pairs(descriptor.attempt_contracts) != _attempt_contract_pairs(
        contribution.attempt_contracts
    ):
        raise PluginContractError("attempt contract declarations disagree with contribution")


def realize_plugin(descriptor: PluginDescriptor, contribution: PluginContribution) -> PluginContribution:
    """Authenticate a realized contribution against its descriptor, including Attempt contracts."""

    validate_contribution(descriptor, contribution)
    return contribution


__all__ = [
    "ActivityState",
    "AttemptContractRef",
    "CapabilityBindingContribution",
    "CandidateFile",
    "CandidateWriteSet",
    "CommitValidator",
    "DirectoryIdentity",
    "DurableEffectHandler",
    "EffectApplyResult",
    "EffectIntent",
    "EffectPolicy",
    "EffectReconcileResult",
    "EffectRegistration",
    "FailureKind",
    "FrozenModel",
    "InvocationMetadata",
    "InvocationWorkspaceBinding",
    "PluginContribution",
    "PluginContractError",
    "PluginDependency",
    "PluginDescriptor",
    "PathBearingFile",
    "PathWriteSet",
    "PluginProvider",
    "PreparedWorkspaceRef",
    "PromotionReceipt",
    "ProviderSource",
    "RecoverableTaskHandler",
    "RegistryPorts",
    "ResourceContribution",
    "ResourceClaims",
    "ResourceClaimTemplate",
    "SchemaContribution",
    "SealedFile",
    "SealedWriteSet",
    "SecretHandleUnauthorized",
    "SecretPort",
    "StagedFile",
    "StagedWriteSet",
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
    "TaskWorkspaceBinding",
    "TaskWorkspaceIdentity",
    "ValidationContext",
    "ValidationResult",
    "WorkspaceProvider",
    "realize_plugin",
    "run_validators",
    "validate_contribution",
]
