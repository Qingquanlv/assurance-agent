from __future__ import annotations

import hashlib
import hmac
import os
import struct
from collections.abc import Iterable, Mapping
from typing import Literal, Protocol, runtime_checkable

from pydantic import Field, field_validator, model_validator

from graph_engine.canonical import canonical_digest
from graph_engine.composition.lock import (
    TASK_HOST_IMPLEMENTATION_ID,
    TASK_HOST_WIRE_SCHEMA_VERSION,
    pinned_execution_host_lock,
)
from graph_engine.errors import GraphEngineError
from graph_engine.identifiers import IdentifierError, validate_qualified_id
from graph_engine.plugin_api import (
    ActivityPhase,
    DirectoryIdentity,
    FrozenModel,
    SecretHandleUnauthorized,
    SecretPort,
    TaskActivityCancelResult,
    TaskActivityReconcileResult,
    TaskActivitySnapshot,
    TaskOutcome,
    TaskRequest,
    TaskWorkspaceIdentity,
)


_SHA256_PATTERN = r"^[0-9a-f]{64}$"
ATTEMPT_ROOT_CAPABILITY_ID: Literal["graph.engine.attempt-root"] = "graph.engine.attempt-root"
HostOperation = Literal["execute", "reconcile", "cancel"]


def authorized_secret_port(authorized: Mapping[str, bytes]) -> SecretPort:
    """Return a non-enumerable resolver for exact authorized handles only."""

    material = {str(handle): bytes(value) for handle, value in authorized.items()}

    class _Port:
        __slots__ = ()

        def resolve(self, handle: str) -> bytes:
            try:
                return bytes(material[handle])
            except KeyError as error:
                raise SecretHandleUnauthorized(f"unauthorized secret handle: {handle}") from error

    return _Port()


def _qualified_id(value: str, kind: str) -> str:
    try:
        return validate_qualified_id(value)
    except IdentifierError as error:
        raise ValueError(f"invalid {kind}: {value!r}") from error


class AttemptRootDescriptor(FrozenModel):
    schema_version: Literal["2"] = "2"
    capability_id: Literal["graph.engine.attempt-root"] = ATTEMPT_ROOT_CAPABILITY_ID
    workspace_identity: TaskWorkspaceIdentity
    project_root_identity: DirectoryIdentity
    write_root_identity: DirectoryIdentity
    project_root_digest: str = Field(pattern=_SHA256_PATTERN)
    write_root_digest: str = Field(pattern=_SHA256_PATTERN)
    baseline_digest: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _authenticate_identity(self) -> AttemptRootDescriptor:
        if self.project_root_digest != self.workspace_identity.project_digest:
            raise ValueError("attempt root project digest disagrees with workspace identity")
        if self.write_root_digest != self.workspace_identity.write_root_digest:
            raise ValueError("attempt root write digest disagrees with workspace identity")
        if self.project_root_identity.identity_digest != self.project_root_digest:
            raise ValueError("attempt root project stat evidence disagrees with its digest")
        if self.write_root_identity.identity_digest != self.write_root_digest:
            raise ValueError("attempt root write stat evidence disagrees with its digest")
        expected_baseline = canonical_digest(
            [item.model_dump(mode="json") for item in self.workspace_identity.baseline_files]
        )
        if self.baseline_digest != expected_baseline:
            raise ValueError("attempt root baseline digest is not canonical")
        return self


class _AttemptBoundIdentity(FrozenModel):
    attempt_key_digest: str = Field(pattern=_SHA256_PATTERN)
    authorization_id: str = Field(pattern=_SHA256_PATTERN)
    fencing_token: int = Field(ge=1)
    phase: ActivityPhase
    workspace_identity_digest: str = Field(pattern=_SHA256_PATTERN)
    request_digest: str = Field(pattern=_SHA256_PATTERN)
    graph_revision: str = Field(pattern=_SHA256_PATTERN)
    product_lock_digest: str = Field(pattern=_SHA256_PATTERN)
    handler_id: str
    host_implementation_id: str
    host_implementation_digest: str = Field(pattern=_SHA256_PATTERN)
    wire_schema_version: Literal["2"] = TASK_HOST_WIRE_SCHEMA_VERSION

    @field_validator("handler_id")
    @classmethod
    def _validate_handler_id(cls, value: str) -> str:
        return _qualified_id(value, "handler id")

    @field_validator("host_implementation_id")
    @classmethod
    def _validate_host_implementation_id(cls, value: str) -> str:
        return _qualified_id(value, "host implementation id")


class TaskActivityRpcIdentity(_AttemptBoundIdentity):
    invocation_id: str
    task_id: str
    activation_id: str
    attempt: int = Field(ge=1)
    activity_id: str | None = None


class TaskHostCallIdentity(_AttemptBoundIdentity):
    invocation_id: str
    task_id: str
    activation_id: str
    attempt: int = Field(ge=1)
    activity_id: str | None = None
    operation: HostOperation


class _TaskHostCallBase(FrozenModel):
    identity: TaskHostCallIdentity
    capability_id: str
    capability_entrypoint: str = Field(min_length=1)
    request: TaskRequest
    attempt_root: AttemptRootDescriptor
    activity_rpc: TaskActivityRpcIdentity
    authorized_secret_handles: tuple[str, ...] = ()
    timeout_seconds: float = Field(default=30.0, gt=0, le=3600)

    @field_validator("capability_id")
    @classmethod
    def _validate_capability_id(cls, value: str) -> str:
        return _qualified_id(value, "host capability id")

    @field_validator("authorized_secret_handles")
    @classmethod
    def _validate_secret_handles(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(values)) != len(values):
            raise ValueError("authorized secret handles must be unique")
        return tuple(sorted(values))


class TaskHostExecuteCall(_TaskHostCallBase):
    @model_validator(mode="after")
    def _require_execute_operation(self) -> TaskHostExecuteCall:
        if self.identity.operation != "execute":
            raise ValueError("execute call identity operation must be execute")
        return self


class TaskHostReconcileCall(_TaskHostCallBase):
    activity: TaskActivitySnapshot

    @model_validator(mode="after")
    def _require_reconcile_operation(self) -> TaskHostReconcileCall:
        if self.identity.operation != "reconcile":
            raise ValueError("reconcile call identity operation must be reconcile")
        return self


class TaskHostCancelCall(_TaskHostCallBase):
    activity: TaskActivitySnapshot

    @model_validator(mode="after")
    def _require_cancel_operation(self) -> TaskHostCancelCall:
        if self.identity.operation != "cancel":
            raise ValueError("cancel call identity operation must be cancel")
        return self


class TaskHostCallResult(FrozenModel):
    operation: HostOperation
    outcome: TaskOutcome | None = None
    reconcile_result: TaskActivityReconcileResult | None = None
    cancel_result: TaskActivityCancelResult | None = None

    @model_validator(mode="after")
    def _validate_operation_payload(self) -> TaskHostCallResult:
        if self.operation == "execute":
            if self.outcome is None or self.reconcile_result is not None or self.cancel_result is not None:
                raise ValueError("execute result requires outcome only")
        elif self.operation == "reconcile":
            if self.reconcile_result is None or self.outcome is not None or self.cancel_result is not None:
                raise ValueError("reconcile result requires reconcile_result only")
        elif self.outcome is not None or self.reconcile_result is not None or self.cancel_result is None:
            raise ValueError("cancel result requires cancel_result only")
        return self


class TaskHostTerminalReceipt(FrozenModel):
    schema_version: Literal["3"] = "3"
    host_implementation_digest: str = Field(pattern=_SHA256_PATTERN)
    wire_schema_version: Literal["2"] = TASK_HOST_WIRE_SCHEMA_VERSION
    invocation_id: str
    task_id: str
    activation_id: str
    attempt: int = Field(ge=1)
    activity_id: str = Field(min_length=1)
    operation: HostOperation
    attempt_key_digest: str = Field(pattern=_SHA256_PATTERN)
    authorization_id: str = Field(pattern=_SHA256_PATTERN)
    fencing_token: int = Field(ge=1)
    phase: ActivityPhase
    graph_revision: str = Field(pattern=_SHA256_PATTERN)
    product_lock_digest: str = Field(pattern=_SHA256_PATTERN)
    handler_id: str
    request_digest: str = Field(pattern=_SHA256_PATTERN)
    workspace_identity_digest: str = Field(pattern=_SHA256_PATTERN)
    project_root_digest: str = Field(pattern=_SHA256_PATTERN)
    write_root_digest: str = Field(pattern=_SHA256_PATTERN)
    baseline_digest: str = Field(pattern=_SHA256_PATTERN)
    staged_write_set_digest: str = Field(pattern=_SHA256_PATTERN)
    dispatch_fingerprint_digest: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    reference_digest: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    outcome: TaskOutcome
    outcome_digest: str = Field(pattern=_SHA256_PATTERN)
    terminal_proof_digest: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    quiescence_proof_digest: str = Field(pattern=_SHA256_PATTERN)
    host_call_id: int = Field(ge=1)

    @field_validator("handler_id")
    @classmethod
    def _validate_handler_id(cls, value: str) -> str:
        return _qualified_id(value, "handler id")

    @model_validator(mode="after")
    def _validate_outcome_digest(self) -> TaskHostTerminalReceipt:
        expected = canonical_digest(self.outcome.model_dump(mode="json"))
        if self.outcome_digest != expected:
            raise ValueError("terminal receipt requires a canonical outcome digest")
        return self


_ATTEMPT_BOUND_FIELDS = (
    "attempt_key_digest",
    "authorization_id",
    "fencing_token",
    "phase",
    "workspace_identity_digest",
    "request_digest",
    "graph_revision",
    "product_lock_digest",
    "handler_id",
    "host_implementation_id",
    "host_implementation_digest",
    "wire_schema_version",
)


def attempt_bound_identity_fields(
    identity: TaskActivityRpcIdentity | TaskHostCallIdentity | TaskHostTerminalReceipt,
) -> dict[str, object]:
    return {name: getattr(identity, name) for name in _ATTEMPT_BOUND_FIELDS}


def identities_agree(
    left: TaskActivityRpcIdentity | TaskHostCallIdentity,
    right: TaskActivityRpcIdentity | TaskHostCallIdentity,
) -> bool:
    shared = (
        "invocation_id",
        "task_id",
        "activation_id",
        "attempt",
        "activity_id",
        *_ATTEMPT_BOUND_FIELDS,
    )
    return all(getattr(left, name) == getattr(right, name) for name in shared)


def current_bound_identity(
    *,
    attempt_key_digest: str,
    authorization_id: str,
    workspace_identity_digest: str,
    request_digest: str,
    graph_revision: str,
    product_lock_digest: str,
    handler_id: str,
    fencing_token: int = 1,
    phase: ActivityPhase = "runtime",
) -> dict[str, object]:
    host = pinned_execution_host_lock()
    return {
        "attempt_key_digest": attempt_key_digest,
        "authorization_id": authorization_id,
        "fencing_token": fencing_token,
        "phase": phase,
        "workspace_identity_digest": workspace_identity_digest,
        "request_digest": request_digest,
        "graph_revision": graph_revision,
        "product_lock_digest": product_lock_digest,
        "handler_id": handler_id,
        "host_implementation_id": host.implementation_id,
        "host_implementation_digest": host.implementation_digest,
        "wire_schema_version": TASK_HOST_WIRE_SCHEMA_VERSION,
    }


@runtime_checkable
class TaskExecutionHost(Protocol):
    async def execute(self, call: TaskHostExecuteCall) -> TaskHostCallResult: ...

    async def reconcile(self, call: TaskHostReconcileCall) -> TaskHostCallResult: ...

    async def cancel(self, call: TaskHostCancelCall) -> TaskHostCallResult: ...

    def read_terminal_receipts(
        self, identity: TaskHostCallIdentity
    ) -> tuple[TaskHostTerminalReceipt, ...]: ...


class TaskHostProtocolError(GraphEngineError):
    """Raised when a host call violates the closed engine-owned transport."""


TASK_HOST_WIRE_MAGIC = b"GEHOST01"
_MAX_WIRE_FRAME_BYTES = 16 * 1024 * 1024
_MAX_STDERR_BYTES = 64 * 1024


def derive_wire_session_key(*, call_digest: str, wire_schema_version: str) -> bytes:
    material = f"{wire_schema_version}:{call_digest}".encode("utf-8")
    return hashlib.sha256(material).digest()


def write_all_bytes(fd: int, data: bytes) -> None:
    """Write every byte to a pipe or socket even when the OS returns a short count."""
    written = 0
    while written < len(data):
        written += os.write(fd, data[written:])


def encode_authenticated_frame(session_key: bytes, payload: bytes) -> bytes:
    if len(payload) > _MAX_WIRE_FRAME_BYTES:
        raise TaskHostProtocolError("wire frame exceeds the bounded payload limit")
    digest = hmac.new(session_key, payload, hashlib.sha256).digest()
    return TASK_HOST_WIRE_MAGIC + struct.pack(">I", len(payload)) + digest + payload


def decode_authenticated_frame(session_key: bytes, buffer: bytes) -> tuple[bytes, bytes]:
    header_size = len(TASK_HOST_WIRE_MAGIC) + 4 + hashlib.sha256().digest_size
    if len(buffer) < header_size:
        raise TaskHostProtocolError("incomplete authenticated wire frame")
    if not buffer.startswith(TASK_HOST_WIRE_MAGIC):
        raise TaskHostProtocolError("wire frame magic mismatch")
    (length,) = struct.unpack(">I", buffer[len(TASK_HOST_WIRE_MAGIC) : len(TASK_HOST_WIRE_MAGIC) + 4])
    if length > _MAX_WIRE_FRAME_BYTES:
        raise TaskHostProtocolError("wire frame exceeds the bounded payload limit")
    digest_offset = len(TASK_HOST_WIRE_MAGIC) + 4
    digest = buffer[digest_offset : digest_offset + hashlib.sha256().digest_size]
    payload_offset = digest_offset + hashlib.sha256().digest_size
    if len(buffer) < payload_offset + length:
        raise TaskHostProtocolError("incomplete authenticated wire frame")
    payload = buffer[payload_offset : payload_offset + length]
    expected = hmac.new(session_key, payload, hashlib.sha256).digest()
    if not hmac.compare_digest(digest, expected):
        raise TaskHostProtocolError("wire frame authentication failed")
    remainder = buffer[payload_offset + length :]
    return payload, remainder


def scan_for_secret_leaks(content: str | bytes, secrets: Iterable[bytes]) -> None:
    haystack = content if isinstance(content, bytes) else content.encode("utf-8")
    for secret in secrets:
        if secret and secret in haystack:
            raise TaskHostProtocolError("authorized secret material leaked into host output")


__all__ = [
    "ATTEMPT_ROOT_CAPABILITY_ID",
    "ActivityPhase",
    "AttemptRootDescriptor",
    "HostOperation",
    "TASK_HOST_IMPLEMENTATION_ID",
    "TASK_HOST_WIRE_SCHEMA_VERSION",
    "TaskActivityRpcIdentity",
    "TaskExecutionHost",
    "TaskHostCancelCall",
    "TaskHostCallIdentity",
    "TaskHostCallResult",
    "TaskHostExecuteCall",
    "TaskHostProtocolError",
    "TaskHostReconcileCall",
    "TaskHostTerminalReceipt",
    "TASK_HOST_WIRE_MAGIC",
    "attempt_bound_identity_fields",
    "current_bound_identity",
    "authorized_secret_port",
    "decode_authenticated_frame",
    "derive_wire_session_key",
    "encode_authenticated_frame",
    "identities_agree",
    "scan_for_secret_leaks",
]
