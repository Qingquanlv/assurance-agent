from __future__ import annotations

from collections.abc import Mapping
from pathlib import PureWindowsPath
from typing import Literal, Protocol, runtime_checkable

from pydantic import Field, field_validator, model_validator

from graph_engine.composition.lock import (
    TASK_HOST_IMPLEMENTATION_ID,
    TASK_HOST_WIRE_SCHEMA_VERSION,
)
from graph_engine.errors import GraphEngineError
from graph_engine.identifiers import IdentifierError, validate_qualified_id
from graph_engine.plugin_api import (
    FrozenModel,
    SecretHandleUnauthorized,
    SecretPort,
    TaskActivityCancelResult,
    TaskActivityReconcileResult,
    TaskActivitySnapshot,
    TaskOutcome,
    TaskRequest,
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


def _reject_host_path(value: str, kind: str) -> str:
    windows_path = PureWindowsPath(value)
    if (
        not value
        or "/" in value
        or "\\" in value
        or value in {".", ".."}
        or windows_path.is_absolute()
        or bool(windows_path.drive)
    ):
        raise ValueError(f"{kind} must not contain a host path")
    return value


class AttemptRootDescriptor(FrozenModel):
    capability_id: Literal["graph.engine.attempt-root"] = ATTEMPT_ROOT_CAPABILITY_ID
    attempt_directory_id: str
    layout_schema_version: Literal["1"] = "1"

    @field_validator("attempt_directory_id")
    @classmethod
    def _validate_attempt_directory_id(cls, value: str) -> str:
        return _reject_host_path(value, "attempt directory id")


class TaskActivityRpcIdentity(FrozenModel):
    invocation_id: str
    task_id: str
    activation_id: str
    attempt: int = Field(ge=1)
    activity_id: str | None = None


class TaskHostCallIdentity(FrozenModel):
    invocation_id: str
    task_id: str
    activation_id: str
    attempt: int = Field(ge=1)
    activity_id: str | None = None
    operation: HostOperation
    host_implementation_id: str
    host_implementation_digest: str = Field(pattern=_SHA256_PATTERN)
    wire_schema_version: Literal["1"] = TASK_HOST_WIRE_SCHEMA_VERSION

    @field_validator("host_implementation_id")
    @classmethod
    def _validate_host_implementation_id(cls, value: str) -> str:
        return _qualified_id(value, "host implementation id")


class _TaskHostCallBase(FrozenModel):
    identity: TaskHostCallIdentity
    capability_id: str
    capability_entrypoint: str = Field(min_length=1)
    request: TaskRequest
    attempt_root: AttemptRootDescriptor
    activity_rpc: TaskActivityRpcIdentity
    authorized_secret_handles: tuple[str, ...] = ()

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
    host_call_id: str = Field(min_length=1)
    identity: TaskHostCallIdentity
    request_digest: str = Field(pattern=_SHA256_PATTERN)
    workspace_identity_digest: str = Field(pattern=_SHA256_PATTERN)
    dispatch_fingerprint_digest: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    reference_digest: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    outcome: TaskOutcome
    outcome_digest: str = Field(pattern=_SHA256_PATTERN)
    proof_digest: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    quiescence_digest: str = Field(pattern=_SHA256_PATTERN)
    host_implementation_digest: str = Field(pattern=_SHA256_PATTERN)
    wire_schema_version: Literal["1"] = TASK_HOST_WIRE_SCHEMA_VERSION


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


__all__ = [
    "ATTEMPT_ROOT_CAPABILITY_ID",
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
    "authorized_secret_port",
]
