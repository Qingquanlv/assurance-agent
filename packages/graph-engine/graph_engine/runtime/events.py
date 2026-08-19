from __future__ import annotations

from typing import Annotated, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator

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
    input: FrozenJSONValue = None


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
    payload: FrozenJSONValue = None


class InterruptResumed(RuntimeEventModel):
    kind: Literal["interrupt_resumed"] = "interrupt_resumed"
    interrupt_id: str
    payload: FrozenJSONValue = None


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


RuntimeEvent = Annotated[
    InvocationStarted
    | GraphStarted
    | TokenOffered
    | TokenConsumed
    | NodeActivated
    | TaskAttemptStarted
    | TaskAttemptSucceeded
    | TaskAttemptFailed
    | TaskAttemptStopped
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
    "TokenConsumed",
    "TokenOffered",
]
