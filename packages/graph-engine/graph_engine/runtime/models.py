from __future__ import annotations

from typing import TYPE_CHECKING, Literal, NoReturn

from pydantic import BaseModel, ConfigDict, JsonValue

from graph_engine.errors import GraphEngineError
from graph_engine.runtime.events import (
    EventEnvelope,
    GraphCompleted,
    GraphStarted,
    InterruptResumed,
    InvocationFinished,
    InvocationStarted,
    NodeActivated,
    NodeCompleted,
    NodeInterrupted,
    RuntimeFailure,
    TaskAttemptFailed,
    TaskAttemptStarted,
    TaskAttemptStopped,
    TaskAttemptSucceeded,
    TokenConsumed,
    TokenOffered,
)

if TYPE_CHECKING:
    from graph_engine.canonical import JSONValue
else:
    JSONValue = JsonValue


class ProjectionError(GraphEngineError):
    """Raised when an event stream describes an impossible runtime transition."""


_FROZEN = ConfigDict(frozen=True, extra="forbid", strict=True, allow_inf_nan=False)


class ProjectionModel(BaseModel):
    model_config = _FROZEN


class GraphInstanceRecord(ProjectionModel):
    graph_instance_id: str
    graph_id: str
    parent_graph_instance_id: str | None
    parent_node_id: str | None
    status: Literal["running", "completed"] = "running"
    input: JSONValue = None
    output: JSONValue = None


class TokenRecord(ProjectionModel):
    token_id: str
    graph_instance_id: str
    source: str | None
    target: str
    payload: JSONValue
    consumed_by: str | None = None


class AttemptRecord(ProjectionModel):
    attempt: int
    lease_expires_at: str
    status: Literal["running", "succeeded", "failed", "stopped"] = "running"
    output: JSONValue = None
    failure: RuntimeFailure | None = None
    stop_reason: str | None = None


class ActivationRecord(ProjectionModel):
    activation_id: str
    graph_instance_id: str
    node_id: str
    token_ids: tuple[str, ...]
    status: Literal["active", "completed", "interrupted"] = "active"
    attempts: tuple[AttemptRecord, ...] = ()
    output: JSONValue = None


class PendingInterrupt(ProjectionModel):
    interrupt_id: str
    activation_id: str
    payload: JSONValue = None


class InvocationProjection(ProjectionModel):
    status: Literal["not_started", "running", "succeeded", "failed", "stopped"] = "not_started"
    invocation_id: str | None = None
    product_digest: str | None = None
    entrypoint: str | None = None
    graph_instances: tuple[GraphInstanceRecord, ...] = ()
    offered_tokens: tuple[TokenRecord, ...] = ()
    consumed_tokens: tuple[str, ...] = ()
    activations: tuple[ActivationRecord, ...] = ()
    pending_interrupt: PendingInterrupt | None = None
    terminal_reason: str | None = None


def _fail(seq: int, message: str) -> NoReturn:
    raise ProjectionError(f"event {seq}: {message}")


def fold_events(envelopes: tuple[EventEnvelope, ...]) -> InvocationProjection:
    """Purely derive invocation state from a complete, ordered event stream."""
    projection = InvocationProjection()
    expected_seq = 1

    for envelope in envelopes:
        if envelope.seq != expected_seq:
            _fail(envelope.seq, f"expected sequence {expected_seq}, found {envelope.seq}")
        if not envelope.has_valid_digest():
            _fail(envelope.seq, "envelope digest mismatch")
        expected_seq += 1
        event = envelope.event

        if isinstance(event, InvocationStarted):
            if projection.status != "not_started":
                _fail(envelope.seq, "invocation has already started")
            projection = projection.model_copy(
                update={
                    "status": "running",
                    "invocation_id": event.invocation_id,
                    "product_digest": event.product_digest,
                    "entrypoint": event.entrypoint,
                }
            )
            continue

        if projection.status == "not_started":
            _fail(envelope.seq, "event occurred before invocation_started")
        if projection.status != "running":
            _fail(envelope.seq, "event occurred after invocation_finished")

        if isinstance(event, GraphStarted):
            if any(item.graph_instance_id == event.graph_instance_id for item in projection.graph_instances):
                _fail(envelope.seq, f"graph instance {event.graph_instance_id!r} already started")
            graph = GraphInstanceRecord(
                graph_instance_id=event.graph_instance_id,
                graph_id=event.graph_id,
                parent_graph_instance_id=event.parent_graph_instance_id,
                parent_node_id=event.parent_node_id,
                input=event.input,
            )
            projection = projection.model_copy(
                update={"graph_instances": (*projection.graph_instances, graph)}
            )
        elif isinstance(event, TokenOffered):
            if any(item.token_id == event.token_id for item in projection.offered_tokens):
                _fail(envelope.seq, f"token {event.token_id!r} already offered")
            token = TokenRecord(
                token_id=event.token_id,
                graph_instance_id=event.graph_instance_id,
                source=event.source,
                target=event.target,
                payload=event.payload,
            )
            projection = projection.model_copy(update={"offered_tokens": (*projection.offered_tokens, token)})
        elif isinstance(event, TokenConsumed):
            token = next(
                (item for item in projection.offered_tokens if item.token_id == event.token_id), None
            )
            if token is None:
                _fail(envelope.seq, f"token {event.token_id!r} was not offered")
            if token.consumed_by is not None:
                _fail(envelope.seq, f"token {event.token_id!r} already consumed")
            if token.graph_instance_id != event.graph_instance_id:
                _fail(envelope.seq, f"token {event.token_id!r} belongs to another graph instance")
            updated = tuple(
                item.model_copy(update={"consumed_by": event.node_id})
                if item.token_id == event.token_id
                else item
                for item in projection.offered_tokens
            )
            projection = projection.model_copy(
                update={
                    "offered_tokens": updated,
                    "consumed_tokens": (*projection.consumed_tokens, event.token_id),
                }
            )
        elif isinstance(event, NodeActivated):
            if any(item.activation_id == event.activation_id for item in projection.activations):
                _fail(envelope.seq, f"activation {event.activation_id!r} already exists")
            if len(set(event.token_ids)) != len(event.token_ids):
                _fail(envelope.seq, "activation contains duplicate token ids")
            for token_id in event.token_ids:
                token = next((item for item in projection.offered_tokens if item.token_id == token_id), None)
                if token is None or token.consumed_by != event.node_id:
                    _fail(envelope.seq, f"activation token {token_id!r} was not consumed by the node")
            activation = ActivationRecord(
                activation_id=event.activation_id,
                graph_instance_id=event.graph_instance_id,
                node_id=event.node_id,
                token_ids=event.token_ids,
            )
            projection = projection.model_copy(update={"activations": (*projection.activations, activation)})
        elif isinstance(event, TaskAttemptStarted):
            activation = _activation(projection, event.activation_id, envelope.seq)
            if activation.status != "active":
                _fail(envelope.seq, "attempt started for a non-active activation")
            if activation.attempts and activation.attempts[-1].status == "running":
                _fail(envelope.seq, "activation already has an active attempt")
            expected_attempt = len(activation.attempts) + 1
            if event.attempt != expected_attempt:
                _fail(envelope.seq, f"expected attempt {expected_attempt}, found {event.attempt}")
            attempt = AttemptRecord(
                attempt=event.attempt,
                lease_expires_at=event.lease_expires_at,
            )
            projection = _replace_activation(
                projection,
                activation.model_copy(update={"attempts": (*activation.attempts, attempt)}),
            )
        elif isinstance(event, TaskAttemptSucceeded | TaskAttemptFailed | TaskAttemptStopped):
            activation = _activation(projection, event.activation_id, envelope.seq)
            if not activation.attempts or activation.attempts[-1].status != "running":
                _fail(envelope.seq, "attempt outcome without a matching active attempt")
            attempt = activation.attempts[-1]
            if attempt.attempt != event.attempt:
                _fail(envelope.seq, "attempt outcome without a matching active attempt")
            if isinstance(event, TaskAttemptSucceeded):
                attempt = attempt.model_copy(update={"status": "succeeded", "output": event.output})
            elif isinstance(event, TaskAttemptFailed):
                attempt = attempt.model_copy(update={"status": "failed", "failure": event.failure})
            else:
                attempt = attempt.model_copy(
                    update={"status": "stopped", "stop_reason": event.reason, "output": event.output}
                )
            activation = activation.model_copy(update={"attempts": (*activation.attempts[:-1], attempt)})
            projection = _replace_activation(projection, activation)
        elif isinstance(event, NodeCompleted):
            activation = _activation(projection, event.activation_id, envelope.seq)
            if activation.status != "active":
                _fail(envelope.seq, "node completed from a non-active state")
            if activation.attempts and activation.attempts[-1].status == "running":
                _fail(envelope.seq, "node completed with an active attempt")
            projection = _replace_activation(
                projection,
                activation.model_copy(update={"status": "completed", "output": event.output}),
            )
        elif isinstance(event, NodeInterrupted):
            activation = _activation(projection, event.activation_id, envelope.seq)
            if activation.status != "active":
                _fail(envelope.seq, "node interrupted from a non-active state")
            if projection.pending_interrupt is not None:
                _fail(envelope.seq, "another interrupt is already pending")
            projection = _replace_activation(
                projection, activation.model_copy(update={"status": "interrupted"})
            ).model_copy(
                update={
                    "pending_interrupt": PendingInterrupt(
                        interrupt_id=event.interrupt_id,
                        activation_id=event.activation_id,
                        payload=event.payload,
                    )
                }
            )
        elif isinstance(event, InterruptResumed):
            pending = projection.pending_interrupt
            if pending is None or pending.interrupt_id != event.interrupt_id:
                _fail(envelope.seq, "resume does not match the pending interrupt")
            activation = _activation(projection, pending.activation_id, envelope.seq)
            projection = _replace_activation(
                projection, activation.model_copy(update={"status": "active"})
            ).model_copy(update={"pending_interrupt": None})
        elif isinstance(event, GraphCompleted):
            graph = next(
                (
                    item
                    for item in projection.graph_instances
                    if item.graph_instance_id == event.graph_instance_id
                ),
                None,
            )
            if graph is None:
                _fail(envelope.seq, f"graph instance {event.graph_instance_id!r} was not started")
            if graph.status == "completed":
                _fail(envelope.seq, f"graph instance {event.graph_instance_id!r} already completed")
            graphs = tuple(
                item.model_copy(update={"status": "completed", "output": event.output})
                if item.graph_instance_id == event.graph_instance_id
                else item
                for item in projection.graph_instances
            )
            projection = projection.model_copy(update={"graph_instances": graphs})
        elif isinstance(event, InvocationFinished):
            if event.invocation_id != projection.invocation_id:
                _fail(envelope.seq, "invocation_finished id does not match invocation_started")
            if projection.pending_interrupt is not None:
                _fail(envelope.seq, "invocation finished while an interrupt is pending")
            if any(
                item.attempts and item.attempts[-1].status == "running" for item in projection.activations
            ):
                _fail(envelope.seq, "invocation finished with an active attempt")
            projection = projection.model_copy(
                update={"status": event.status, "terminal_reason": event.terminal_reason}
            )

    return projection


def _activation(projection: InvocationProjection, activation_id: str, seq: int) -> ActivationRecord:
    activation = next((item for item in projection.activations if item.activation_id == activation_id), None)
    if activation is None:
        _fail(seq, f"activation {activation_id!r} does not exist")
    return activation


def _replace_activation(
    projection: InvocationProjection, replacement: ActivationRecord
) -> InvocationProjection:
    activations = tuple(
        replacement if item.activation_id == replacement.activation_id else item
        for item in projection.activations
    )
    return projection.model_copy(update={"activations": activations})


__all__ = [
    "ActivationRecord",
    "AttemptRecord",
    "GraphInstanceRecord",
    "InvocationProjection",
    "PendingInterrupt",
    "ProjectionError",
    "TokenRecord",
    "fold_events",
]
