from __future__ import annotations

from typing import Annotated, Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.plugin_api import FailureKind, TaskFailure
from graph_engine.runtime.frozen_json import FrozenJSONValue


_STRICT_FROZEN = ConfigDict(
    frozen=True,
    extra="forbid",
    strict=True,
    allow_inf_nan=False,
)
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
MAX_EVENT_SEQUENCE = 9_999_999_999


class RuntimeEventModel(BaseModel):
    model_config = _STRICT_FROZEN


RuntimeFailure = TaskFailure


class InvocationStarted(RuntimeEventModel):
    kind: Literal["invocation_started"] = "invocation_started"
    invocation_id: str
    product_digest: str = Field(pattern=_SHA256_PATTERN)
    entrypoint: str


class GraphStarted(RuntimeEventModel):
    kind: Literal["graph_started"] = "graph_started"
    graph_instance_id: str
    graph_id: str
    parent_graph_instance_id: str | None = None
    parent_node_id: str | None = None
    parent_activation_id: str | None = None
    input: FrozenJSONValue = None

    @model_validator(mode="after")
    def _validate_parent_binding(self) -> Self:
        parent = (
            self.parent_graph_instance_id,
            self.parent_node_id,
            self.parent_activation_id,
        )
        if any(value is not None for value in parent) and any(value is None for value in parent):
            raise ValueError("graph parent fields must be present together")
        if self.parent_activation_id is not None:
            expected = canonical_digest(
                {
                    "parent_activation_id": self.parent_activation_id,
                    "graph_id": self.graph_id,
                }
            )
            if self.graph_instance_id != expected:
                raise ValueError("child graph instance id is not canonical for its parent")
        elif self.graph_instance_id != self.graph_id:
            raise ValueError("root graph instance id must equal its graph id")
        return self


class TokenOffered(RuntimeEventModel):
    kind: Literal["token_offered"] = "token_offered"
    token_id: str
    graph_instance_id: str
    source: str | None
    target: str
    payload: FrozenJSONValue


class TokenConsumed(RuntimeEventModel):
    kind: Literal["token_consumed"] = "token_consumed"
    token_id: str
    graph_instance_id: str
    node_id: str


class NodeActivated(RuntimeEventModel):
    kind: Literal["node_activated"] = "node_activated"
    activation_id: str
    graph_instance_id: str
    node_id: str
    token_ids: tuple[str, ...]


class TaskAttemptStarted(RuntimeEventModel):
    kind: Literal["task_attempt_started"] = "task_attempt_started"
    activation_id: str
    attempt: int = Field(ge=1)
    lease_expires_at: str


class TaskLeaseAcquired(RuntimeEventModel):
    kind: Literal["task_lease_acquired"] = "task_lease_acquired"
    task_id: str
    activation_id: str
    attempt: int = Field(ge=1)
    owner_id: str = Field(min_length=1)
    acquired_at: float
    heartbeat_at: float
    expires_at: float

    @model_validator(mode="after")
    def _validate_times(self) -> Self:
        if self.heartbeat_at != self.acquired_at or self.expires_at < self.heartbeat_at:
            raise ValueError("lease acquisition timestamps are inconsistent")
        return self


class TaskLeaseHeartbeat(RuntimeEventModel):
    kind: Literal["task_lease_heartbeat"] = "task_lease_heartbeat"
    task_id: str
    activation_id: str
    attempt: int = Field(ge=1)
    owner_id: str = Field(min_length=1)
    heartbeat_at: float
    expires_at: float

    @model_validator(mode="after")
    def _validate_times(self) -> Self:
        if self.expires_at < self.heartbeat_at:
            raise ValueError("lease heartbeat expiry precedes heartbeat time")
        return self


class TaskAttemptSucceeded(RuntimeEventModel):
    kind: Literal["task_attempt_succeeded"] = "task_attempt_succeeded"
    activation_id: str
    attempt: int = Field(ge=1)
    output: FrozenJSONValue


class TaskAttemptFailed(RuntimeEventModel):
    kind: Literal["task_attempt_failed"] = "task_attempt_failed"
    activation_id: str
    attempt: int = Field(ge=1)
    failure: TaskFailure


class TaskAttemptStopped(RuntimeEventModel):
    kind: Literal["task_attempt_stopped"] = "task_attempt_stopped"
    activation_id: str
    attempt: int = Field(ge=1)
    reason: str
    output: FrozenJSONValue = None


class HeadAdvanced(RuntimeEventModel):
    kind: Literal["head_advanced"] = "head_advanced"
    task_id: str
    activation_id: str
    attempt: int = Field(ge=1)
    previous_tree_id: str = Field(pattern=_SHA256_PATTERN)
    tree_id: str = Field(pattern=_SHA256_PATTERN)


class NodeCompleted(RuntimeEventModel):
    kind: Literal["node_completed"] = "node_completed"
    activation_id: str
    output: FrozenJSONValue = None


class NodeFailed(RuntimeEventModel):
    kind: Literal["node_failed"] = "node_failed"
    activation_id: str
    failure: TaskFailure


class NodeInterrupted(RuntimeEventModel):
    kind: Literal["node_interrupted"] = "node_interrupted"
    activation_id: str
    interrupt_id: str
    graph_instance_id: str
    reason: str
    actions: tuple[str, ...]
    input: FrozenJSONValue
    payload: FrozenJSONValue = None

    @model_validator(mode="after")
    def _validate_interrupt(self) -> Self:
        if not self.reason:
            raise ValueError("interrupt reason must not be empty")
        if not self.actions or any(not action for action in self.actions):
            raise ValueError("interrupt actions must be non-empty")
        if len(set(self.actions)) != len(self.actions):
            raise ValueError("interrupt actions must be unique")
        return self


class InterruptResumed(RuntimeEventModel):
    kind: Literal["interrupt_resumed"] = "interrupt_resumed"
    interrupt_id: str
    action: str
    payload: FrozenJSONValue


class GraphCompleted(RuntimeEventModel):
    kind: Literal["graph_completed"] = "graph_completed"
    graph_instance_id: str
    output: FrozenJSONValue = None


class GraphFailed(RuntimeEventModel):
    kind: Literal["graph_failed"] = "graph_failed"
    graph_instance_id: str
    reason: str


class InvocationFinished(RuntimeEventModel):
    kind: Literal["invocation_finished"] = "invocation_finished"
    invocation_id: str
    status: Literal["succeeded", "failed", "stopped"]
    terminal_reason: str | None = None

    @model_validator(mode="after")
    def _validate_terminal_reason(self) -> Self:
        if self.status == "succeeded" and self.terminal_reason is not None:
            raise ValueError("successful invocation cannot have a terminal reason")
        return self


RuntimeEvent = Annotated[
    InvocationStarted
    | GraphStarted
    | TokenOffered
    | TokenConsumed
    | NodeActivated
    | TaskAttemptStarted
    | TaskLeaseAcquired
    | TaskLeaseHeartbeat
    | TaskAttemptSucceeded
    | TaskAttemptFailed
    | TaskAttemptStopped
    | HeadAdvanced
    | NodeCompleted
    | NodeFailed
    | NodeInterrupted
    | InterruptResumed
    | GraphCompleted
    | GraphFailed
    | InvocationFinished,
    Field(discriminator="kind"),
]


class EventEnvelope(BaseModel):
    model_config = _STRICT_FROZEN

    seq: int = Field(ge=1, le=MAX_EVENT_SEQUENCE)
    event: RuntimeEvent
    event_sha256: str = Field(pattern=_SHA256_PATTERN)

    @field_validator("seq")
    @classmethod
    def _reject_boolean_sequence(cls, value: int) -> int:
        if isinstance(value, bool):
            raise ValueError("sequence must be an integer")
        return value

    @classmethod
    def from_event(cls, seq: int, event: RuntimeEvent) -> EventEnvelope:
        payload = _envelope_digest_payload(seq, event)
        return cls(seq=seq, event=event, event_sha256=canonical_digest(payload))

    def has_valid_digest(self) -> bool:
        return self.event_sha256 == canonical_digest(_envelope_digest_payload(self.seq, self.event))


def _envelope_digest_payload(seq: int, event: RuntimeEvent) -> JSONValue:
    return cast(
        JSONValue,
        {
            "seq": seq,
            "event": event.model_dump(mode="json"),
        },
    )


__all__ = [
    "EventEnvelope",
    "FailureKind",
    "GraphCompleted",
    "GraphFailed",
    "GraphStarted",
    "HeadAdvanced",
    "InterruptResumed",
    "InvocationFinished",
    "InvocationStarted",
    "NodeActivated",
    "NodeCompleted",
    "NodeFailed",
    "NodeInterrupted",
    "RuntimeEvent",
    "RuntimeFailure",
    "TaskFailure",
    "TaskAttemptFailed",
    "TaskAttemptStarted",
    "TaskAttemptStopped",
    "TaskAttemptSucceeded",
    "TaskLeaseAcquired",
    "TaskLeaseHeartbeat",
    "TokenConsumed",
    "TokenOffered",
]
