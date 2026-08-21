from __future__ import annotations

from typing import Literal, NoReturn, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from graph_engine.canonical import canonical_digest
from graph_engine.errors import GraphEngineError
from graph_engine.plugin_api import ResourceClaims, TaskFailure
from graph_engine.runtime.events import (
    EventEnvelope,
    GraphCompleted,
    GraphFailed,
    GraphStarted,
    HeadAdvanced,
    InterruptResumed,
    InvocationFinished,
    InvocationStarted,
    NodeActivated,
    NodeCompleted,
    NodeFailed,
    NodeInterrupted,
    RuntimeEvent,
    TaskAttemptFailed,
    TaskAttemptStarted,
    TaskAttemptStopped,
    TaskAttemptSucceeded,
    TaskLeaseAcquired,
    TaskLeaseHeartbeat,
    TokenConsumed,
    TokenOffered,
)
from graph_engine.runtime.frozen_json import FrozenJSONValue


class ProjectionError(GraphEngineError):
    """Raised when an event stream describes an impossible runtime transition."""


_FROZEN = ConfigDict(frozen=True, extra="forbid", strict=True, allow_inf_nan=False)


class ProjectionModel(BaseModel):
    model_config = _FROZEN


class ValidationReceipt(ProjectionModel):
    validator_id: str
    accepted: bool
    reason: str | None = None

    @model_validator(mode="after")
    def _validate_reason(self) -> Self:
        if self.accepted and self.reason is not None:
            raise ValueError("reason is allowed only for rejected validation receipts")
        if not self.accepted and not self.reason:
            raise ValueError("rejected validation receipts require a non-empty reason")
        return self


class CommitResult(ProjectionModel):
    committed: bool
    receipts: tuple[ValidationReceipt, ...] = ()

    @model_validator(mode="after")
    def _validate_commit_outcome(self) -> Self:
        all_accepted = all(receipt.accepted for receipt in self.receipts)
        if self.committed != all_accepted:
            raise ValueError("commit succeeds exactly when all validation receipts are accepted")
        return self


class GraphInstanceRecord(ProjectionModel):
    graph_instance_id: str
    graph_id: str
    parent_graph_instance_id: str | None
    parent_node_id: str | None
    parent_activation_id: str | None = None
    status: Literal["running", "completed", "failed"] = "running"
    input: FrozenJSONValue = None
    output: FrozenJSONValue = None
    failure_reason: str | None = None

    @model_validator(mode="after")
    def _validate_outcome(self) -> Self:
        if (self.failure_reason is not None) != (self.status == "failed"):
            raise ValueError("graph failure reason is required exactly for failed status")
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


class TokenRecord(ProjectionModel):
    token_id: str
    graph_instance_id: str
    source: str | None
    target: str
    payload: FrozenJSONValue
    consumed_by: str | None = None
    activation_id: str | None = None


class AttemptRecord(ProjectionModel):
    attempt: int = Field(ge=1)
    lease_expires_at: str
    status: Literal["running", "succeeded", "failed", "stopped"] = "running"
    output: FrozenJSONValue = None
    failure: TaskFailure | None = None
    stop_reason: str | None = None
    lease_task_id: str | None = None
    lease_owner_id: str | None = None
    lease_acquired_at: float | None = None
    lease_heartbeat_at: float | None = None
    lease_expires_at_value: float | None = None
    committed_tree_id: str | None = None

    @model_validator(mode="after")
    def _validate_outcome(self) -> Self:
        if (self.failure is not None) != (self.status == "failed"):
            raise ValueError("attempt failure is required exactly for failed status")
        if self.status == "stopped":
            if not self.stop_reason:
                raise ValueError("stopped attempt requires a stop reason")
        elif self.stop_reason is not None:
            raise ValueError("stop reason is allowed only for stopped status")
        lease_values = (
            self.lease_task_id,
            self.lease_owner_id,
            self.lease_acquired_at,
            self.lease_heartbeat_at,
            self.lease_expires_at_value,
        )
        if any(value is not None for value in lease_values) and any(value is None for value in lease_values):
            raise ValueError("attempt lease fields must be present together")
        if (
            self.lease_acquired_at is not None
            and self.lease_heartbeat_at is not None
            and self.lease_expires_at_value is not None
            and not (self.lease_acquired_at <= self.lease_heartbeat_at <= self.lease_expires_at_value)
        ):
            raise ValueError("attempt lease timestamps are inconsistent")
        return self


class ActivationRecord(ProjectionModel):
    activation_id: str
    graph_instance_id: str
    node_id: str
    token_ids: tuple[str, ...]
    status: Literal["active", "completed", "failed", "interrupted", "stopped"] = "active"
    attempts: tuple[AttemptRecord, ...] = ()
    output: FrozenJSONValue = None
    failure: TaskFailure | None = None
    interrupt_id: str | None = None
    interrupt_reason: str | None = None
    interrupt_actions: tuple[str, ...] = ()
    interrupt_input: FrozenJSONValue = None
    interrupt_resumed: bool = False
    interrupt_action: str | None = None
    interrupt_payload: FrozenJSONValue = None
    structural_failure: bool = False

    @model_validator(mode="after")
    def _validate_outcome(self) -> Self:
        if (self.failure is not None) != (self.status == "failed"):
            raise ValueError("activation failure is required exactly for failed status")
        if self.interrupt_resumed and self.attempts:
            raise ValueError("task activation cannot have interrupt resume history")
        interrupt_metadata = (
            self.interrupt_id,
            self.interrupt_reason,
            self.interrupt_actions or None,
        )
        if any(value is not None for value in interrupt_metadata) and any(
            value is None for value in interrupt_metadata
        ):
            raise ValueError("interrupt activation metadata must be present together")
        if self.interrupt_id is not None and self.attempts:
            raise ValueError("task activation cannot have interrupt metadata")
        if self.interrupt_resumed != (self.interrupt_action is not None):
            raise ValueError("interrupt resume action is required exactly for resumed interrupts")
        if self.structural_failure and (self.status != "failed" or self.attempts):
            raise ValueError("structural failure requires a failed activation without attempts")
        return self


class PendingInterrupt(ProjectionModel):
    interrupt_id: str
    activation_id: str
    graph_instance_id: str
    reason: str
    actions: tuple[str, ...]
    input: FrozenJSONValue
    payload: FrozenJSONValue = None


class PlannedTask(ProjectionModel):
    invocation_id: str
    task_id: str
    activation_id: str
    graph_instance_id: str
    node_id: str
    capability_id: str
    attempt: int
    input: FrozenJSONValue
    prior_failure: TaskFailure | None = None
    timeout_seconds: float = Field(gt=0)
    resources: ResourceClaims = ResourceClaims()
    validators: tuple[str, ...] = ()
    topology_rank: int = Field(ge=0)
    declaration_index: int = Field(ge=0)


class PlanResult(ProjectionModel):
    tasks: tuple[PlannedTask, ...] = ()
    events: tuple[RuntimeEvent, ...] = ()
    terminal: Literal["succeeded", "failed", "stopped", "interrupted"] | None = None
    reason: str | None = None

    @model_validator(mode="after")
    def _validate_terminal_reason(self) -> Self:
        if self.terminal is None and self.reason is not None:
            raise ValueError("non-terminal plans cannot have a terminal reason")
        if self.terminal == "succeeded" and self.reason is not None:
            raise ValueError("successful plans cannot have a terminal reason")
        return self


class InvocationProjection(ProjectionModel):
    status: Literal["not_started", "running", "succeeded", "failed", "stopped"] = "not_started"
    invocation_id: str | None = None
    lock_digest: str | None = None
    entrypoint: str | None = None
    graph_instances: tuple[GraphInstanceRecord, ...] = ()
    offered_tokens: tuple[TokenRecord, ...] = ()
    consumed_tokens: tuple[str, ...] = ()
    activations: tuple[ActivationRecord, ...] = ()
    pending_interrupt: PendingInterrupt | None = None
    terminal_reason: str | None = None
    head_tree_id: str | None = None

    @model_validator(mode="after")
    def _validate_semantics(self) -> Self:
        identity = (self.invocation_id, self.lock_digest, self.entrypoint)
        if self.status == "not_started":
            if any(value is not None for value in identity) or any(
                (
                    self.graph_instances,
                    self.offered_tokens,
                    self.consumed_tokens,
                    self.activations,
                    self.pending_interrupt,
                    self.terminal_reason,
                    self.head_tree_id,
                )
            ):
                raise ValueError("not_started projection cannot contain runtime state")
            return self
        if any(value is None for value in identity):
            raise ValueError("started projection requires complete invocation identity")
        if self.status == "running" and self.terminal_reason is not None:
            raise ValueError("running projection cannot have a terminal reason")
        if self.status == "succeeded" and self.terminal_reason is not None:
            raise ValueError("successful projection cannot have a terminal reason")

        _require_unique((item.graph_instance_id for item in self.graph_instances), "graph instance")
        _require_unique((item.token_id for item in self.offered_tokens), "token")
        _require_unique(self.consumed_tokens, "consumed token")
        _require_unique((item.activation_id for item in self.activations), "activation")

        graph_ids = {item.graph_instance_id for item in self.graph_instances}
        activation_by_id = {item.activation_id: item for item in self.activations}
        token_by_id = {item.token_id: item for item in self.offered_tokens}
        for graph in self.graph_instances:
            if graph.parent_graph_instance_id is not None:
                if graph.parent_graph_instance_id == graph.graph_instance_id:
                    raise ValueError("graph instance cannot parent itself")
                if graph.parent_graph_instance_id not in graph_ids:
                    raise ValueError("graph instance has a dangling parent")
            if graph.parent_activation_id is not None:
                parent_activation = activation_by_id.get(graph.parent_activation_id)
                if (
                    parent_activation is None
                    or parent_activation.graph_instance_id != graph.parent_graph_instance_id
                    or parent_activation.node_id != graph.parent_node_id
                ):
                    raise ValueError("graph instance has a dangling parent activation")
        for token_id in self.consumed_tokens:
            token = token_by_id.get(token_id)
            if token is None or token.consumed_by is None:
                raise ValueError("consumed token list has a dangling token")
        if {item.token_id for item in self.offered_tokens if item.consumed_by is not None} != set(
            self.consumed_tokens
        ):
            raise ValueError("consumed token records disagree with consumed token ids")
        for activation in self.activations:
            _validate_attempt_history(activation)
            graph = next(
                (
                    item
                    for item in self.graph_instances
                    if item.graph_instance_id == activation.graph_instance_id
                ),
                None,
            )
            if graph is not None and graph.status == "completed" and activation.status != "completed":
                raise ValueError("completed graph contains an unsettled activation")
            for token_id in activation.token_ids:
                token = token_by_id.get(token_id)
                if token is None or token.activation_id != activation.activation_id:
                    raise ValueError("activation has a dangling or unclaimed token")
                if (
                    token.graph_instance_id != activation.graph_instance_id
                    or token.consumed_by != activation.node_id
                ):
                    raise ValueError("activation token graph/node binding is inconsistent")
        for token in self.offered_tokens:
            if token.activation_id is not None:
                activation = activation_by_id.get(token.activation_id)
                if activation is None or token.token_id not in activation.token_ids:
                    raise ValueError("token has a dangling activation claim")
        if self.pending_interrupt is not None:
            activation = activation_by_id.get(self.pending_interrupt.activation_id)
            if activation is None or activation.status != "interrupted":
                raise ValueError("pending interrupt has a dangling activation")
            if self.pending_interrupt.graph_instance_id != activation.graph_instance_id:
                raise ValueError("pending interrupt graph instance disagrees with activation")
        if self.status in {"failed", "stopped"}:
            if self.pending_interrupt is not None:
                raise ValueError("terminal projection cannot retain an interrupt")
            if any(item.attempts and item.attempts[-1].status == "running" for item in self.activations):
                raise ValueError("terminal projection cannot retain an active attempt")
        if self.status == "failed" and any(item.status == "stopped" for item in self.activations):
            raise ValueError("failed projection cannot contain stopped activations")
        if self.status == "succeeded":
            if not self.graph_instances or any(item.status != "completed" for item in self.graph_instances):
                raise ValueError("succeeded projection requires completed graph instances")
            if any(item.status != "completed" for item in self.activations):
                raise ValueError("succeeded projection requires completed activations")
            if self.pending_interrupt is not None:
                raise ValueError("succeeded projection cannot retain an interrupt")
            if any(_token_is_unsettled_for_success(self, token) for token in self.offered_tokens):
                raise ValueError("succeeded projection contains an unsettled token")
        return self


class FoldCursor(ProjectionModel):
    """Immutable cursor for applying contiguous event-envelope batches once."""

    projection: InvocationProjection = Field(default_factory=InvocationProjection)
    next_seq: int = Field(default=1, ge=1)

    def advance(self, envelopes: tuple[EventEnvelope, ...]) -> FoldCursor:
        """Purely advance this cursor through one ordered envelope batch."""
        return _advance_fold(self, envelopes)


def _require_unique(values: object, kind: str) -> None:
    materialized = tuple(values)  # type: ignore[arg-type]
    if len(materialized) != len(set(materialized)):
        raise ValueError(f"projection contains duplicate {kind} ids")


def _validate_attempt_history(activation: ActivationRecord) -> None:
    for expected, attempt in enumerate(activation.attempts, start=1):
        if attempt.attempt != expected:
            raise ValueError("activation attempt numbers must be contiguous")
        if expected < len(activation.attempts) and attempt.status != "failed":
            raise ValueError("only a failed attempt can be followed by a retry")
    if activation.status == "completed" and activation.attempts:
        if activation.attempts[-1].status != "succeeded":
            raise ValueError("completed task activation requires a successful attempt")
    if activation.status == "failed":
        if activation.attempts and activation.attempts[-1].status != "failed":
            raise ValueError("failed activation requires a failed attempt")
        if not activation.attempts and not activation.structural_failure:
            raise ValueError("failed activation requires failed task or structural history")
    if activation.status == "stopped":
        if not activation.attempts or activation.attempts[-1].status != "stopped":
            raise ValueError("stopped activation requires a stopped attempt")
    if activation.status == "interrupted" and activation.attempts:
        if activation.attempts[-1].status == "running":
            raise ValueError("interrupted activation cannot retain a running attempt")


def _fail(seq: int, message: str) -> NoReturn:
    raise ProjectionError(f"event {seq}: {message}")


def fold_events(envelopes: tuple[EventEnvelope, ...]) -> InvocationProjection:
    """Purely derive invocation state from a complete, ordered event stream."""
    return FoldCursor().advance(envelopes).projection


def _advance_fold(
    cursor: FoldCursor,
    envelopes: tuple[EventEnvelope, ...],
) -> FoldCursor:
    projection = cursor.projection
    expected_seq = cursor.next_seq

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
                    "lock_digest": event.lock_digest,
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
            if event.parent_graph_instance_id is not None:
                _ensure_graph_open(projection, event.parent_graph_instance_id, envelope.seq)
            if event.parent_activation_id is not None:
                parent_activation = _activation(projection, event.parent_activation_id, envelope.seq)
                if (
                    parent_activation.status != "active"
                    or parent_activation.graph_instance_id != event.parent_graph_instance_id
                    or parent_activation.node_id != event.parent_node_id
                ):
                    _fail(envelope.seq, "child graph parent activation binding is invalid")
                expected_graph_instance_id = canonical_digest(
                    {
                        "parent_activation_id": event.parent_activation_id,
                        "graph_id": event.graph_id,
                    }
                )
                if event.graph_instance_id != expected_graph_instance_id:
                    _fail(envelope.seq, "child graph instance id is not canonical")
            graph = GraphInstanceRecord(
                graph_instance_id=event.graph_instance_id,
                graph_id=event.graph_id,
                parent_graph_instance_id=event.parent_graph_instance_id,
                parent_node_id=event.parent_node_id,
                parent_activation_id=event.parent_activation_id,
                input=event.input,
            )
            projection = projection.model_copy(
                update={"graph_instances": (*projection.graph_instances, graph)}
            )
        elif isinstance(event, TokenOffered):
            _ensure_graph_open_if_known(projection, event.graph_instance_id, envelope.seq)
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
            _ensure_graph_open_if_known(projection, event.graph_instance_id, envelope.seq)
            token = next(
                (item for item in projection.offered_tokens if item.token_id == event.token_id), None
            )
            if token is None:
                _fail(envelope.seq, f"token {event.token_id!r} was not offered")
            if token.consumed_by is not None:
                _fail(envelope.seq, f"token {event.token_id!r} already consumed")
            if token.graph_instance_id != event.graph_instance_id:
                _fail(envelope.seq, f"token {event.token_id!r} belongs to another graph instance")
            if token.target != event.node_id:
                _fail(envelope.seq, f"token {event.token_id!r} does not target node {event.node_id!r}")
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
            _ensure_graph_open_if_known(projection, event.graph_instance_id, envelope.seq)
            if any(item.activation_id == event.activation_id for item in projection.activations):
                _fail(envelope.seq, f"activation {event.activation_id!r} already exists")
            if len(set(event.token_ids)) != len(event.token_ids):
                _fail(envelope.seq, "activation contains duplicate token ids")
            for token_id in event.token_ids:
                token = next((item for item in projection.offered_tokens if item.token_id == token_id), None)
                if token is None or token.consumed_by != event.node_id:
                    _fail(envelope.seq, f"activation token {token_id!r} was not consumed by the node")
                if token.graph_instance_id != event.graph_instance_id:
                    _fail(envelope.seq, f"token {token_id!r} belongs to another graph instance")
                if token.activation_id is not None:
                    _fail(
                        envelope.seq,
                        f"token {token_id!r} already claimed by activation {token.activation_id!r}",
                    )
            activation = ActivationRecord(
                activation_id=event.activation_id,
                graph_instance_id=event.graph_instance_id,
                node_id=event.node_id,
                token_ids=event.token_ids,
            )
            projection = projection.model_copy(
                update={
                    "offered_tokens": tuple(
                        item.model_copy(update={"activation_id": event.activation_id})
                        if item.token_id in event.token_ids
                        else item
                        for item in projection.offered_tokens
                    ),
                    "activations": (*projection.activations, activation),
                }
            )
        elif isinstance(event, TaskAttemptStarted):
            activation = _activation(projection, event.activation_id, envelope.seq)
            _ensure_graph_open_if_known(projection, activation.graph_instance_id, envelope.seq)
            if activation.status != "active":
                _fail(envelope.seq, "attempt started for a non-active activation")
            if activation.attempts:
                prior_status = activation.attempts[-1].status
                if prior_status == "running":
                    _fail(envelope.seq, "activation already has an active attempt")
                if prior_status != "failed":
                    _fail(envelope.seq, f"cannot retry after a {prior_status} attempt")
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
        elif isinstance(event, TaskLeaseAcquired):
            activation = _activation(projection, event.activation_id, envelope.seq)
            if not activation.attempts or activation.attempts[-1].status != "running":
                _fail(envelope.seq, "lease acquired without a matching active attempt")
            attempt = activation.attempts[-1]
            if attempt.attempt != event.attempt or attempt.lease_owner_id is not None:
                _fail(envelope.seq, "lease acquired without a matching active attempt")
            if event.heartbeat_at != event.acquired_at or event.expires_at < event.heartbeat_at:
                _fail(envelope.seq, "lease acquisition timestamps are inconsistent")
            attempt = attempt.model_copy(
                update={
                    "lease_owner_id": event.owner_id,
                    "lease_task_id": event.task_id,
                    "lease_acquired_at": event.acquired_at,
                    "lease_heartbeat_at": event.heartbeat_at,
                    "lease_expires_at_value": event.expires_at,
                }
            )
            projection = _replace_activation(
                projection,
                activation.model_copy(update={"attempts": (*activation.attempts[:-1], attempt)}),
            )
        elif isinstance(event, TaskLeaseHeartbeat):
            activation = _activation(projection, event.activation_id, envelope.seq)
            if not activation.attempts or activation.attempts[-1].status != "running":
                _fail(envelope.seq, "lease heartbeat without a matching active attempt")
            attempt = activation.attempts[-1]
            if (
                attempt.attempt != event.attempt
                or attempt.lease_task_id != event.task_id
                or attempt.lease_owner_id != event.owner_id
            ):
                _fail(envelope.seq, "lease heartbeat does not match the active lease")
            if (
                attempt.lease_heartbeat_at is None
                or event.heartbeat_at < attempt.lease_heartbeat_at
                or (
                    attempt.lease_expires_at_value is not None
                    and event.heartbeat_at > attempt.lease_expires_at_value
                )
                or event.expires_at < event.heartbeat_at
            ):
                _fail(envelope.seq, "lease heartbeat timestamps are inconsistent")
            attempt = attempt.model_copy(
                update={
                    "lease_heartbeat_at": event.heartbeat_at,
                    "lease_expires_at_value": event.expires_at,
                }
            )
            projection = _replace_activation(
                projection,
                activation.model_copy(update={"attempts": (*activation.attempts[:-1], attempt)}),
            )
        elif isinstance(event, TaskAttemptSucceeded | TaskAttemptFailed | TaskAttemptStopped):
            activation = _activation(projection, event.activation_id, envelope.seq)
            _ensure_graph_open_if_known(projection, activation.graph_instance_id, envelope.seq)
            if activation.status != "active":
                _fail(envelope.seq, "attempt outcome for a non-active activation")
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
            activation = activation.model_copy(
                update={
                    "attempts": (*activation.attempts[:-1], attempt),
                    "status": "stopped" if isinstance(event, TaskAttemptStopped) else "active",
                }
            )
            projection = _replace_activation(projection, activation)
        elif isinstance(event, NodeCompleted):
            activation = _activation(projection, event.activation_id, envelope.seq)
            _ensure_graph_open_if_known(projection, activation.graph_instance_id, envelope.seq)
            if activation.status != "active":
                _fail(envelope.seq, "node completed from a non-active state")
            if activation.attempts and activation.attempts[-1].status != "succeeded":
                _fail(envelope.seq, "task node completed without a successful latest attempt")
            projection = _replace_activation(
                projection,
                activation.model_copy(update={"status": "completed", "output": event.output}),
            )
        elif isinstance(event, NodeFailed):
            activation = _activation(projection, event.activation_id, envelope.seq)
            _ensure_graph_open_if_known(projection, activation.graph_instance_id, envelope.seq)
            if activation.status != "active":
                _fail(envelope.seq, "node failed from a non-active state")
            if activation.attempts:
                if activation.attempts[-1].status != "failed":
                    _fail(envelope.seq, "node failed without a failed latest attempt")
                if activation.attempts[-1].failure != event.failure:
                    _fail(envelope.seq, "node failure disagrees with latest attempt failure")
            elif not any(
                graph.parent_activation_id == activation.activation_id and graph.status == "failed"
                for graph in projection.graph_instances
            ):
                _fail(envelope.seq, "structural node failed without a failed child graph")
            projection = _replace_activation(
                projection,
                activation.model_copy(
                    update={
                        "status": "failed",
                        "failure": event.failure,
                        "structural_failure": not activation.attempts,
                    }
                ),
            )
        elif isinstance(event, NodeInterrupted):
            activation = _activation(projection, event.activation_id, envelope.seq)
            _ensure_graph_open_if_known(projection, activation.graph_instance_id, envelope.seq)
            if activation.status != "active":
                _fail(envelope.seq, "node interrupted from a non-active state")
            if activation.attempts:
                _fail(envelope.seq, "node interrupted after task-attempt history")
            if projection.pending_interrupt is not None:
                _fail(envelope.seq, "another interrupt is already pending")
            if event.graph_instance_id != activation.graph_instance_id:
                _fail(envelope.seq, "interrupt graph instance disagrees with activation")
            projection = _replace_activation(
                projection,
                activation.model_copy(
                    update={
                        "status": "interrupted",
                        "interrupt_id": event.interrupt_id,
                        "interrupt_reason": event.reason,
                        "interrupt_actions": event.actions,
                        "interrupt_input": event.input,
                    }
                ),
            ).model_copy(
                update={
                    "pending_interrupt": PendingInterrupt(
                        interrupt_id=event.interrupt_id,
                        activation_id=event.activation_id,
                        graph_instance_id=event.graph_instance_id,
                        reason=event.reason,
                        actions=event.actions,
                        input=event.input,
                        payload=event.payload,
                    )
                }
            )
        elif isinstance(event, HeadAdvanced):
            activation = _activation(projection, event.activation_id, envelope.seq)
            _ensure_graph_open_if_known(projection, activation.graph_instance_id, envelope.seq)
            if activation.status != "active":
                _fail(envelope.seq, "HEAD advanced for a non-active activation")
            if not activation.attempts or activation.attempts[-1].status != "succeeded":
                _fail(envelope.seq, "HEAD advanced without a successful task attempt")
            attempt = activation.attempts[-1]
            if attempt.attempt != event.attempt or attempt.lease_task_id != event.task_id:
                _fail(envelope.seq, "HEAD advance does not match the successful attempt")
            if attempt.committed_tree_id is not None:
                _fail(envelope.seq, "successful task attempt already advanced HEAD")
            if projection.head_tree_id is not None and projection.head_tree_id != event.previous_tree_id:
                _fail(envelope.seq, "HEAD advance previous tree does not match projection")
            attempt = attempt.model_copy(update={"committed_tree_id": event.tree_id})
            projection = _replace_activation(
                projection,
                activation.model_copy(update={"attempts": (*activation.attempts[:-1], attempt)}),
            ).model_copy(update={"head_tree_id": event.tree_id})
        elif isinstance(event, InterruptResumed):
            pending = projection.pending_interrupt
            if pending is None or pending.interrupt_id != event.interrupt_id:
                _fail(envelope.seq, "resume does not match the pending interrupt")
            if event.action not in pending.actions:
                _fail(envelope.seq, "resume action is not allowed by the pending interrupt")
            activation = _activation(projection, pending.activation_id, envelope.seq)
            _ensure_graph_open_if_known(projection, activation.graph_instance_id, envelope.seq)
            projection = _replace_activation(
                projection,
                activation.model_copy(
                    update={
                        "status": "active",
                        "interrupt_resumed": True,
                        "interrupt_action": event.action,
                        "interrupt_payload": event.payload,
                    }
                ),
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
            if graph.status == "failed":
                _fail(envelope.seq, f"graph instance {event.graph_instance_id!r} already failed")
            for token in projection.offered_tokens:
                if token.graph_instance_id != event.graph_instance_id:
                    continue
                activation = (
                    _find_activation(projection, token.activation_id)
                    if token.activation_id is not None
                    else None
                )
                if activation is None or activation.status != "completed":
                    _fail(envelope.seq, f"graph completed with unclaimed token {token.token_id!r}")
            if any(
                item.graph_instance_id == event.graph_instance_id and item.status != "completed"
                for item in projection.activations
            ):
                _fail(envelope.seq, "graph completed with an unsettled activation")
            if any(
                item.parent_graph_instance_id == event.graph_instance_id and item.status != "completed"
                for item in projection.graph_instances
            ):
                _fail(envelope.seq, "graph completed with an unfinished child graph")
            if projection.pending_interrupt is not None:
                pending_activation = _activation(
                    projection, projection.pending_interrupt.activation_id, envelope.seq
                )
                if pending_activation.graph_instance_id == event.graph_instance_id:
                    _fail(envelope.seq, "graph completed with a pending interrupt")
            graphs = tuple(
                item.model_copy(update={"status": "completed", "output": event.output})
                if item.graph_instance_id == event.graph_instance_id
                else item
                for item in projection.graph_instances
            )
            projection = projection.model_copy(update={"graph_instances": graphs})
        elif isinstance(event, GraphFailed):
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
            if graph.status != "running":
                _fail(envelope.seq, f"graph instance {event.graph_instance_id!r} is not running")
            if any(
                item.graph_instance_id == event.graph_instance_id
                and item.attempts
                and item.attempts[-1].status == "running"
                for item in projection.activations
            ):
                _fail(envelope.seq, "graph failed with an active attempt")
            graphs = tuple(
                item.model_copy(update={"status": "failed", "failure_reason": event.reason})
                if item.graph_instance_id == event.graph_instance_id
                else item
                for item in projection.graph_instances
            )
            projection = projection.model_copy(update={"graph_instances": graphs})
        elif isinstance(event, InvocationFinished):
            if event.invocation_id != projection.invocation_id:
                _fail(envelope.seq, "invocation_finished id does not match invocation_started")
            if event.status == "succeeded":
                if event.terminal_reason is not None:
                    _fail(envelope.seq, "successful invocation cannot have a terminal reason")
                if not projection.graph_instances or any(
                    item.status != "completed" for item in projection.graph_instances
                ):
                    _fail(envelope.seq, "successful invocation finished with a running graph")
                if any(item.status != "completed" for item in projection.activations):
                    _fail(envelope.seq, "successful invocation finished with an unsettled activation")
                if projection.pending_interrupt is not None:
                    _fail(envelope.seq, "successful invocation finished with a pending interrupt")
                if any(
                    _token_is_unsettled_for_success(projection, token) for token in projection.offered_tokens
                ):
                    _fail(envelope.seq, "successful invocation finished with an unsettled token")
            elif event.status == "failed":
                _require_no_live_attempt_or_interrupt(projection, envelope.seq, "failed")
                if any(item.status == "stopped" for item in projection.activations):
                    _fail(envelope.seq, "failed invocation contains a stopped activation")
            else:
                _require_no_live_attempt_or_interrupt(projection, envelope.seq, "stopped")
            projection = projection.model_copy(
                update={"status": event.status, "terminal_reason": event.terminal_reason}
            )

    try:
        validated = InvocationProjection.model_validate_json(projection.model_dump_json(), strict=True)
    except ValueError as error:
        raise ProjectionError(f"fold produced an invalid projection: {error}") from error
    return FoldCursor(projection=validated, next_seq=expected_seq)


def _activation(projection: InvocationProjection, activation_id: str, seq: int) -> ActivationRecord:
    activation = _find_activation(projection, activation_id)
    if activation is None:
        _fail(seq, f"activation {activation_id!r} does not exist")
    return activation


def _find_activation(projection: InvocationProjection, activation_id: str | None) -> ActivationRecord | None:
    return next(
        (item for item in projection.activations if item.activation_id == activation_id),
        None,
    )


def _token_is_unsettled_for_success(projection: InvocationProjection, token: TokenRecord) -> bool:
    graph = next(
        (item for item in projection.graph_instances if item.graph_instance_id == token.graph_instance_id),
        None,
    )
    activation = _find_activation(projection, token.activation_id)
    return (
        graph is None or graph.status != "completed" or activation is None or activation.status != "completed"
    )


def _ensure_graph_open(
    projection: InvocationProjection, graph_instance_id: str, seq: int
) -> GraphInstanceRecord:
    graph = next(
        (item for item in projection.graph_instances if item.graph_instance_id == graph_instance_id),
        None,
    )
    if graph is None:
        _fail(seq, f"graph instance {graph_instance_id!r} was not started")
    if graph.status == "completed":
        _fail(seq, f"graph instance {graph_instance_id!r} already completed")
    if graph.status == "failed":
        _fail(seq, f"graph instance {graph_instance_id!r} already failed")
    return graph


def _ensure_graph_open_if_known(projection: InvocationProjection, graph_instance_id: str, seq: int) -> None:
    graph = next(
        (item for item in projection.graph_instances if item.graph_instance_id == graph_instance_id),
        None,
    )
    if graph is not None and graph.status == "completed":
        _fail(seq, f"graph instance {graph_instance_id!r} already completed")
    if graph is not None and graph.status == "failed":
        _fail(seq, f"graph instance {graph_instance_id!r} already failed")


def _require_no_live_attempt_or_interrupt(
    projection: InvocationProjection, seq: int, terminal_status: str
) -> None:
    if projection.pending_interrupt is not None:
        _fail(seq, f"{terminal_status} invocation retains a pending interrupt")
    if any(item.attempts and item.attempts[-1].status == "running" for item in projection.activations):
        _fail(seq, f"{terminal_status} invocation retains an active attempt")


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
    "CommitResult",
    "FoldCursor",
    "GraphInstanceRecord",
    "InvocationProjection",
    "PendingInterrupt",
    "PlannedTask",
    "PlanResult",
    "ProjectionError",
    "TokenRecord",
    "ValidationReceipt",
    "fold_events",
]
