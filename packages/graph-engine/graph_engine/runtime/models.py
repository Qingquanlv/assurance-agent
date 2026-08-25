from __future__ import annotations

from typing import Literal, NoReturn, Self, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.errors import GraphEngineError
from graph_engine.plugin_api import (
    ResourceClaims,
    StagedWriteSet,
    TaskActivitySnapshot,
    TaskFailure,
    TaskWorkspaceIdentity,
)
from graph_engine.runtime.events import (
    EffectApplyStarted,
    EffectIntentCommitted,
    EffectReceiptRecorded,
    EventEnvelope,
    GraphCompleted,
    GraphFailed,
    GraphStarted,
    InterruptResumed,
    InvocationFinished,
    InvocationStarted,
    NodeActivated,
    NodeCompleted,
    NodeFailed,
    NodeInterrupted,
    RuntimeEvent,
    TaskActivityBound,
    TaskActivityCancelRequested,
    TaskActivityDispatchStarted,
    TaskActivityPrepared,
    TaskActivityTerminalObserved,
    TaskAttemptFailed,
    TaskAttemptStarted,
    TaskAttemptStopped,
    TaskAttemptSucceeded,
    TaskCommitPrepared,
    TaskLeaseAcquired,
    TaskLeaseAdopted,
    TaskLeaseHeartbeat,
    TaskPromotionCompleted,
    TokenConsumed,
    TokenOffered,
)
from graph_engine.runtime.frozen_json import FrozenJSONValue, thaw_json


class ProjectionError(GraphEngineError):
    """Raised when an event stream describes an impossible runtime transition."""


def attempt_identity_digest(
    invocation_id: str,
    task_id: str,
    activation_id: str,
    attempt: int,
) -> str:
    return canonical_digest(
        {
            "activation_id": activation_id,
            "attempt": attempt,
            "invocation_id": invocation_id,
            "task_id": task_id,
        }
    )


def attempt_directory_id(
    invocation_id: str,
    task_id: str,
    activation_id: str,
    attempt: int,
) -> str:
    return attempt_identity_digest(invocation_id, task_id, activation_id, attempt)


def activity_id_for_attempt(
    invocation_id: str,
    task_id: str,
    activation_id: str,
    attempt: int,
) -> str:
    return canonical_digest(
        {
            "activation_id": activation_id,
            "attempt": attempt,
            "invocation_id": invocation_id,
            "kind": "activity",
            "task_id": task_id,
        }
    )


_FROZEN = ConfigDict(frozen=True, extra="forbid", strict=True, allow_inf_nan=False)


class ProjectionModel(BaseModel):
    model_config = _FROZEN


AttemptStatus = Literal[
    "running",
    "promotion_pending",
    "effect_pending",
    "succeeded",
    "failed",
    "stopped",
]
EffectStatus = Literal["committed", "applying", "applied", "permanently_failed"]
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_LIVE_ATTEMPT_STATUSES = {"running", "promotion_pending", "effect_pending"}
_LIVE_ACTIVITY_STATES = {"prepared", "dispatch_started", "bound"}


class PreparedTaskCommit(ProjectionModel):
    task_id: str
    activation_id: str
    attempt: int = Field(ge=1)
    output: FrozenJSONValue = None
    workspace_identity: TaskWorkspaceIdentity
    staged_write_set: StagedWriteSet
    staged_write_set_digest: str = Field(pattern=_SHA256_PATTERN)
    promotion_receipt_digest: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    effect_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_effect_ids(self) -> Self:
        if self.workspace_identity.identity_digest != self.staged_write_set.identity_digest:
            raise ValueError("prepared staged write set belongs to another workspace")
        if self.staged_write_set_digest != self.staged_write_set.staged_digest:
            raise ValueError("prepared staged write-set digest is not canonical")
        if any(not effect_id for effect_id in self.effect_ids):
            raise ValueError("prepared effect ids must be non-empty")
        if len(set(self.effect_ids)) != len(self.effect_ids):
            raise ValueError("prepared effect ids must be unique")
        return self


class EffectRecord(ProjectionModel):
    effect_id: str
    task_id: str
    activation_id: str
    task_attempt: int = Field(ge=1)
    index: int = Field(ge=0)
    kind: str
    payload: FrozenJSONValue
    idempotency_key: str = Field(pattern=_SHA256_PATTERN)
    status: EffectStatus = "committed"
    apply_attempts: int = Field(default=0, ge=0)
    receipt: FrozenJSONValue = None
    failure: TaskFailure | None = None

    @model_validator(mode="after")
    def _validate_outcome(self) -> Self:
        if (self.failure is not None) != (self.status == "permanently_failed"):
            raise ValueError("effect failure is required exactly for permanently_failed status")
        if self.status != "applied" and self.receipt is not None:
            raise ValueError("effect receipt is allowed only for applied status")
        if self.status == "applying" and self.apply_attempts < 1:
            raise ValueError("applying effect requires at least one apply attempt")
        return self


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
    status: AttemptStatus = "running"
    activity: TaskActivitySnapshot | None = None
    output: FrozenJSONValue = None
    failure: TaskFailure | None = None
    stop_reason: str | None = None
    lease_task_id: str | None = None
    lease_owner_id: str | None = None
    lease_acquired_at: float | None = None
    lease_heartbeat_at: float | None = None
    lease_expires_at_value: float | None = None
    prepared_commit: PreparedTaskCommit | None = None

    @model_validator(mode="after")
    def _validate_outcome(self) -> Self:
        if (self.failure is not None) != (self.status == "failed"):
            raise ValueError("attempt failure is required exactly for failed status")
        if self.status == "stopped":
            if not self.stop_reason:
                raise ValueError("stopped attempt requires a stop reason")
        elif self.stop_reason is not None:
            raise ValueError("stop reason is allowed only for stopped status")
        if self.status in {"promotion_pending", "effect_pending"} and self.prepared_commit is None:
            raise ValueError("pending attempt requires a prepared commit")
        if self.prepared_commit is not None and self.prepared_commit.attempt != self.attempt:
            raise ValueError("prepared commit does not match the attempt")
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


RecoveryDecisionKind = Literal[
    "execute_same_attempt",
    "adopt_same_attempt",
    "promote_same_attempt",
    "finalize_failure_then_retry_policy",
    "block",
]
ReconcileStatus = Literal["not_dispatched", "running", "terminal", "absent", "indeterminate"]


class ActivityRecoveryDecision(ProjectionModel):
    activity_id: str
    task_id: str
    activation_id: str
    attempt: int = Field(ge=1)
    decision: RecoveryDecisionKind
    reconcile_status: ReconcileStatus | None = None


class RecoveryResult(ProjectionModel):
    decisions: tuple[ActivityRecoveryDecision, ...] = ()


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
    effects: tuple[EffectRecord, ...] = ()
    pending_interrupt: PendingInterrupt | None = None
    terminal_reason: str | None = None

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
                    self.effects,
                    self.pending_interrupt,
                    self.terminal_reason,
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
        _require_unique((item.effect_id for item in self.effects), "effect")

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
        _validate_effect_projection(self, activation_by_id)
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
            if any(
                item.attempts and item.attempts[-1].status in _LIVE_ATTEMPT_STATUSES
                for item in self.activations
            ):
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

    @classmethod
    def initial(cls) -> Self:
        return cls()

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
        if expected < len(activation.attempts):
            if attempt.status != "failed":
                raise ValueError("only a failed attempt can be followed by a retry")
            if attempt.failure is not None and not attempt.failure.retryable:
                raise ValueError("non-retryable failure cannot be followed by a retry")
        if (
            attempt.prepared_commit is not None
            and attempt.prepared_commit.activation_id != activation.activation_id
        ):
            raise ValueError("prepared commit does not match the activation")
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
        if activation.attempts[-1].status in _LIVE_ATTEMPT_STATUSES:
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
                prior = activation.attempts[-1]
                if prior.status in _LIVE_ATTEMPT_STATUSES:
                    _fail(envelope.seq, "activation already has an active attempt")
                if prior.activity is not None and prior.activity.state in _LIVE_ACTIVITY_STATES:
                    _fail(
                        envelope.seq,
                        "cannot start a new attempt while prior activity is live or indeterminate",
                    )
                if prior.status != "failed":
                    _fail(envelope.seq, f"cannot retry after a {prior.status} attempt")
                if prior.failure is not None and not prior.failure.retryable:
                    _fail(envelope.seq, "cannot retry after a non-retryable failure")
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
            if not activation.attempts or activation.attempts[-1].status not in _LIVE_ATTEMPT_STATUSES:
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
        elif isinstance(event, TaskActivityPrepared):
            projection = _fold_task_activity_prepared(projection, event, envelope.seq)
        elif isinstance(event, TaskActivityDispatchStarted):
            projection = _fold_task_activity_dispatch_started(projection, event, envelope.seq)
        elif isinstance(event, TaskActivityBound):
            projection = _fold_task_activity_bound(projection, event, envelope.seq)
        elif isinstance(event, TaskActivityCancelRequested):
            projection = _fold_task_activity_cancel_requested(projection, event, envelope.seq)
        elif isinstance(event, TaskActivityTerminalObserved):
            projection = _fold_task_activity_terminal_observed(projection, event, envelope.seq)
        elif isinstance(event, TaskLeaseAdopted):
            projection = _fold_task_lease_adopted(projection, event, envelope.seq)
        elif isinstance(event, TaskCommitPrepared):
            projection = _fold_task_commit_prepared(projection, event, envelope.seq)
        elif isinstance(event, TaskPromotionCompleted):
            projection = _fold_task_promotion_completed(projection, event, envelope.seq)
        elif isinstance(event, EffectIntentCommitted):
            projection = _fold_effect_intent_committed(projection, event, envelope.seq)
        elif isinstance(event, EffectApplyStarted):
            projection = _fold_effect_apply_started(projection, event, envelope.seq)
        elif isinstance(event, EffectReceiptRecorded):
            projection = _fold_effect_receipt_recorded(projection, event, envelope.seq)
        elif isinstance(event, TaskAttemptSucceeded | TaskAttemptFailed | TaskAttemptStopped):
            projection = _fold_task_attempt_outcome(projection, event, envelope.seq)
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
            _require_no_pending_effects(projection, envelope.seq, event.graph_instance_id)
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
            _require_no_pending_effects(projection, envelope.seq, event.graph_instance_id)
            if any(
                item.graph_instance_id == event.graph_instance_id
                and item.attempts
                and item.attempts[-1].status in _LIVE_ATTEMPT_STATUSES
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
                _require_no_pending_effects(projection, envelope.seq, None)
                _require_no_live_attempt_or_interrupt(projection, envelope.seq, "failed")
                if any(item.status == "stopped" for item in projection.activations):
                    _fail(envelope.seq, "failed invocation contains a stopped activation")
            else:
                _require_no_pending_effects(projection, envelope.seq, None)
                _require_no_live_attempt_or_interrupt(projection, envelope.seq, "stopped")
            projection = projection.model_copy(
                update={"status": event.status, "terminal_reason": event.terminal_reason}
            )

    try:
        validated = InvocationProjection.model_validate_json(projection.model_dump_json(), strict=True)
    except ValueError as error:
        raise ProjectionError(f"fold produced an invalid projection: {error}") from error
    return FoldCursor(projection=validated, next_seq=expected_seq)


def _validate_effect_projection(
    projection: InvocationProjection,
    activation_by_id: dict[str, ActivationRecord],
) -> None:
    for effect in projection.effects:
        activation = activation_by_id.get(effect.activation_id)
        if activation is None:
            raise ValueError("effect has a dangling activation")
        attempt = next(
            (item for item in activation.attempts if item.attempt == effect.task_attempt),
            None,
        )
        if attempt is None or attempt.prepared_commit is None:
            raise ValueError("effect has a dangling prepared commit")
        prepared = attempt.prepared_commit
        if (
            effect.task_id != prepared.task_id
            or effect.index >= len(prepared.effect_ids)
            or prepared.effect_ids[effect.index] != effect.effect_id
        ):
            raise ValueError("effect does not match the prepared commit")
    grouped: dict[tuple[str, int], list[EffectRecord]] = {}
    for effect in projection.effects:
        grouped.setdefault((effect.activation_id, effect.task_attempt), []).append(effect)
    for records in grouped.values():
        ordered = sorted(records, key=lambda item: item.index)
        if [item.index for item in ordered] != list(range(len(ordered))):
            raise ValueError("effect indexes must be contiguous")


def _fold_task_commit_prepared(
    projection: InvocationProjection,
    event: TaskCommitPrepared,
    seq: int,
) -> InvocationProjection:
    activation = _activation(projection, event.activation_id, seq)
    _ensure_graph_open_if_known(projection, activation.graph_instance_id, seq)
    if activation.status != "active":
        _fail(seq, "commit prepared for a non-active activation")
    if not activation.attempts or activation.attempts[-1].status != "running":
        _fail(seq, "commit prepared without a matching active attempt")
    attempt = activation.attempts[-1]
    if attempt.attempt != event.attempt:
        _fail(seq, "commit prepared without a matching active attempt")
    if attempt.lease_task_id is not None and attempt.lease_task_id != event.task_id:
        _fail(seq, "prepared commit does not match the active attempt")
    _require_activity_commit_prepared(attempt, event, seq)
    prepared = PreparedTaskCommit(
        task_id=event.task_id,
        activation_id=event.activation_id,
        attempt=event.attempt,
        output=event.output,
        workspace_identity=event.workspace_identity,
        staged_write_set=event.staged_write_set,
        staged_write_set_digest=event.staged_write_set_digest,
        effect_ids=event.effect_ids,
    )
    attempt = attempt.model_copy(
        update={
            "status": "promotion_pending",
            "output": event.output,
            "prepared_commit": prepared,
        }
    )
    return _replace_activation(
        projection,
        activation.model_copy(update={"attempts": (*activation.attempts[:-1], attempt)}),
    )


def _fold_task_promotion_completed(
    projection: InvocationProjection,
    event: TaskPromotionCompleted,
    seq: int,
) -> InvocationProjection:
    activation = _activation(projection, event.activation_id, seq)
    _ensure_graph_open_if_known(projection, activation.graph_instance_id, seq)
    if activation.status != "active" or not activation.attempts:
        _fail(seq, "promotion completed without a prepared task attempt")
    attempt = activation.attempts[-1]
    prepared = attempt.prepared_commit
    if attempt.status != "promotion_pending" or prepared is None:
        _fail(seq, "promotion completed without a prepared task attempt")
    if (
        attempt.attempt != event.attempt
        or prepared.task_id != event.task_id
        or prepared.staged_write_set_digest != event.staged_write_set_digest
        or prepared.promotion_receipt_digest is not None
    ):
        _fail(seq, "promotion receipt does not match the prepared task attempt")
    prepared = prepared.model_copy(update={"promotion_receipt_digest": event.promotion_receipt_digest})
    attempt = attempt.model_copy(
        update={
            "status": "effect_pending" if prepared.effect_ids else "promotion_pending",
            "prepared_commit": prepared,
        }
    )
    return _replace_activation(
        projection,
        activation.model_copy(update={"attempts": (*activation.attempts[:-1], attempt)}),
    )


def _fold_effect_intent_committed(
    projection: InvocationProjection,
    event: EffectIntentCommitted,
    seq: int,
) -> InvocationProjection:
    activation = _activation(projection, event.activation_id, seq)
    _ensure_graph_open_if_known(projection, activation.graph_instance_id, seq)
    if not activation.attempts:
        _fail(seq, "intent committed without a prepared commit")
    attempt = activation.attempts[-1]
    prepared = attempt.prepared_commit
    if attempt.status != "promotion_pending" or prepared is None:
        _fail(seq, "intent committed without a prepared commit")
    if attempt.attempt != event.attempt:
        _fail(seq, "intent committed without a matching prepared commit")
    if any(item.effect_id == event.effect_id for item in projection.effects):
        _fail(seq, "duplicate effect intent")
    expected_index = sum(
        1
        for item in projection.effects
        if item.activation_id == activation.activation_id and item.task_attempt == attempt.attempt
    )
    if event.index != expected_index:
        _fail(seq, "effect intent index is not contiguous")
    if event.index >= len(prepared.effect_ids) or event.effect_id != prepared.effect_ids[event.index]:
        _fail(seq, "effect id does not match the prepared commit")
    expected_key = _effect_idempotency_key(
        projection.lock_digest,
        event.effect_id,
        event.effect_kind,
        event.payload,
    )
    if event.idempotency_key != expected_key:
        _fail(seq, "idempotency key does not match the committed intent")
    record = EffectRecord(
        effect_id=event.effect_id,
        task_id=prepared.task_id,
        activation_id=event.activation_id,
        task_attempt=event.attempt,
        index=event.index,
        kind=event.effect_kind,
        payload=event.payload,
        idempotency_key=event.idempotency_key,
    )
    return projection.model_copy(update={"effects": (*projection.effects, record)})


def _fold_effect_apply_started(
    projection: InvocationProjection,
    event: EffectApplyStarted,
    seq: int,
) -> InvocationProjection:
    effect = _effect(projection, event.effect_id, seq)
    if effect.status not in {"committed", "applying"}:
        _fail(seq, "effect apply started after the effect settled")
    predecessors = tuple(
        item
        for item in projection.effects
        if item.activation_id == effect.activation_id
        and item.task_attempt == effect.task_attempt
        and item.index < effect.index
    )
    if any(item.status != "applied" for item in predecessors):
        _fail(seq, "effect apply started before a prior receipt")
    expected_attempt = effect.apply_attempts + 1
    if event.apply_attempt != expected_attempt:
        _fail(seq, f"expected apply attempt {expected_attempt}, found {event.apply_attempt}")
    activation = _activation(projection, effect.activation_id, seq)
    _ensure_graph_open_if_known(projection, activation.graph_instance_id, seq)
    attempt = next(
        (item for item in activation.attempts if item.attempt == effect.task_attempt),
        None,
    )
    if (
        attempt is None
        or attempt.status != "effect_pending"
        or attempt.prepared_commit is None
        or attempt.prepared_commit.promotion_receipt_digest is None
    ):
        _fail(seq, "effect apply started before staged output promotion")
    return _replace_effect(
        projection,
        effect.model_copy(update={"status": "applying", "apply_attempts": event.apply_attempt}),
    )


def _fold_effect_receipt_recorded(
    projection: InvocationProjection,
    event: EffectReceiptRecorded,
    seq: int,
) -> InvocationProjection:
    effect = _effect(projection, event.effect_id, seq)
    if effect.status == "applied":
        _fail(seq, "duplicate effect receipt")
    if effect.status != "applying":
        _fail(seq, "effect receipt recorded without apply")
    if event.apply_attempt != effect.apply_attempts:
        _fail(seq, "effect receipt does not match the apply attempt")
    activation = _activation(projection, effect.activation_id, seq)
    _ensure_graph_open_if_known(projection, activation.graph_instance_id, seq)
    return _replace_effect(
        projection,
        effect.model_copy(update={"status": "applied", "receipt": event.receipt}),
    )


def _fold_task_attempt_outcome(
    projection: InvocationProjection,
    event: TaskAttemptSucceeded | TaskAttemptFailed | TaskAttemptStopped,
    seq: int,
) -> InvocationProjection:
    activation = _activation(projection, event.activation_id, seq)
    _ensure_graph_open_if_known(projection, activation.graph_instance_id, seq)
    if activation.status != "active":
        _fail(seq, "attempt outcome for a non-active activation")
    if not activation.attempts:
        _fail(seq, "attempt outcome without a matching active attempt")
    attempt = activation.attempts[-1]
    if attempt.attempt != event.attempt:
        _fail(seq, "attempt outcome without a matching active attempt")
    _require_activity_attempt_outcome(attempt, event, seq)
    if isinstance(event, TaskAttemptSucceeded):
        prepared = attempt.prepared_commit
        if attempt.status == "promotion_pending":
            if prepared is None or prepared.effect_ids or prepared.promotion_receipt_digest is None:
                _fail(seq, "task succeeded before staged output promotion")
            if thaw_json(event.output) != thaw_json(prepared.output):
                _fail(seq, "task success output disagrees with the prepared commit")
            attempt = attempt.model_copy(update={"status": "succeeded", "output": event.output})
        elif attempt.status == "effect_pending":
            if prepared is None or prepared.promotion_receipt_digest is None:
                _fail(seq, "task succeeded before staged output promotion")
            if not _all_effect_receipts_present(projection, activation.activation_id, attempt):
                _fail(seq, "task succeeded before all effect receipts")
            if thaw_json(event.output) != thaw_json(attempt.output):
                _fail(seq, "task success output disagrees with the prepared commit")
            attempt = attempt.model_copy(update={"status": "succeeded", "output": event.output})
        else:
            _fail(seq, "attempt outcome without a matching active attempt")
        assert prepared is not None
        if (
            event.staged_write_set_digest != prepared.staged_write_set_digest
            or event.promotion_receipt_digest != prepared.promotion_receipt_digest
        ):
            _fail(seq, "task success receipt digests disagree with the prepared commit")
        activation = activation.model_copy(update={"attempts": (*activation.attempts[:-1], attempt)})
        return _replace_activation(projection, activation)
    if attempt.status == "effect_pending":
        if isinstance(event, TaskAttemptStopped):
            _fail(seq, "attempt outcome without a matching active attempt")
        if event.failure.retryable:
            _fail(seq, "pending effect failure must be non-retryable")
        return _fail_pending_effect(projection, activation, attempt, event.failure, seq)
    if attempt.status != "running":
        _fail(seq, "attempt outcome without a matching active attempt")
    if isinstance(event, TaskAttemptFailed):
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
    return _replace_activation(projection, activation)


def _require_activity_attempt_outcome(
    attempt: AttemptRecord,
    event: TaskAttemptSucceeded | TaskAttemptFailed | TaskAttemptStopped,
    seq: int,
) -> None:
    activity = attempt.activity
    if activity is None:
        return
    if activity.state != "terminal_observed" or activity.terminal is None:
        _fail(seq, "recoverable attempt outcome requires a terminal activity")
    terminal = activity.terminal
    if isinstance(event, TaskAttemptSucceeded):
        if terminal.status != "succeeded":
            _fail(seq, "task success disagrees with the observed terminal activity")
        if thaw_json(event.output) != thaw_json(terminal.output):
            _fail(seq, "task success output disagrees with the observed terminal activity")
        if activity.staged_write_set_digest != event.staged_write_set_digest:
            _fail(seq, "task success staged write set disagrees with observed activity")
        return
    if terminal.status == "succeeded":
        return
    if isinstance(event, TaskAttemptFailed):
        if terminal.status != "failed" or event.failure != terminal.failure:
            _fail(seq, "task failure disagrees with the observed terminal activity")
        if event.staged_write_set_digest != activity.staged_write_set_digest:
            _fail(seq, "task failure staged write set disagrees with observed activity")
        return
    if terminal.status != "stopped" or event.reason != terminal.stop_reason:
        _fail(seq, "task stop disagrees with the observed terminal activity")
    if event.staged_write_set_digest != activity.staged_write_set_digest:
        _fail(seq, "task stop staged write set disagrees with observed activity")


def _require_activity_commit_prepared(
    attempt: AttemptRecord,
    event: TaskCommitPrepared,
    seq: int,
) -> None:
    activity = attempt.activity
    if activity is None:
        return
    terminal = activity.terminal if activity.state == "terminal_observed" else None
    if terminal is not None and terminal.status == "succeeded":
        if activity.staged_write_set_digest != event.staged_write_set_digest:
            _fail(seq, "prepared staged write set disagrees with observed activity")
        return
    if terminal is not None:
        _fail(seq, "failed terminal activity cannot prepare promotion")
    _fail(seq, "commit prepared before a succeeded terminal activity")


def _fold_task_activity_prepared(
    projection: InvocationProjection,
    event: TaskActivityPrepared,
    seq: int,
) -> InvocationProjection:
    activation = _activation(projection, event.activation_id, seq)
    _ensure_graph_open_if_known(projection, activation.graph_instance_id, seq)
    if activation.status != "active":
        _fail(seq, "activity prepared for a non-active activation")
    if not activation.attempts or activation.attempts[-1].status != "running":
        _fail(seq, "activity prepared without a matching active attempt")
    attempt = activation.attempts[-1]
    if attempt.attempt != event.attempt or attempt.lease_owner_id is None:
        _fail(seq, "activity prepared without a matching active attempt")
    if attempt.activity is not None:
        _fail(seq, "activity is already prepared")
    if attempt.lease_task_id is not None and attempt.lease_task_id != event.task_id:
        _fail(seq, "activity task id does not match the active attempt")
    if _find_attempt_for_activity(projection, event.activity_id) is not None:
        _fail(seq, "activity id is already assigned")
    snapshot = _activity_snapshot(
        seq,
        {
            "activity_id": event.activity_id,
            "request_digest": event.request_digest,
            "workspace_identity": event.workspace_identity,
            "state": "prepared",
        },
    )
    return _replace_latest_attempt(projection, activation, attempt.model_copy(update={"activity": snapshot}))


def _fold_task_activity_dispatch_started(
    projection: InvocationProjection,
    event: TaskActivityDispatchStarted,
    seq: int,
) -> InvocationProjection:
    activation, attempt = _attempt_for_activity(projection, event.activity_id, seq)
    _ensure_graph_open_if_known(projection, activation.graph_instance_id, seq)
    activity = attempt.activity
    if activity is None or activity.state != "prepared":
        _fail(seq, "dispatch transition is already durable")
    if attempt.status not in _LIVE_ATTEMPT_STATUSES:
        _fail(seq, "dispatch started without a matching active attempt")
    snapshot = _activity_snapshot(
        seq,
        {
            **activity.model_dump(mode="python"),
            "state": "dispatch_started",
            "dispatch_fingerprint": thaw_json(event.dispatch_fingerprint),
            "dispatch_fingerprint_digest": event.dispatch_fingerprint_digest,
        },
    )
    return _replace_latest_attempt(projection, activation, attempt.model_copy(update={"activity": snapshot}))


def _fold_task_activity_bound(
    projection: InvocationProjection,
    event: TaskActivityBound,
    seq: int,
) -> InvocationProjection:
    activation, attempt = _attempt_for_activity(projection, event.activity_id, seq)
    _ensure_graph_open_if_known(projection, activation.graph_instance_id, seq)
    activity = attempt.activity
    if activity is None:
        _fail(seq, f"activity {event.activity_id!r} does not exist")
    if activity.state == "bound" or activity.reference is not None:
        _fail(seq, "activity reference is immutable")
    if activity.state != "dispatch_started":
        _fail(seq, "activity cannot bind from the current state")
    if attempt.status not in _LIVE_ATTEMPT_STATUSES:
        _fail(seq, "activity bound without a matching active attempt")
    snapshot = _activity_snapshot(
        seq,
        {
            **activity.model_dump(mode="python"),
            "state": "bound",
            "reference": thaw_json(event.reference),
            "reference_digest": event.reference_digest,
        },
    )
    return _replace_latest_attempt(projection, activation, attempt.model_copy(update={"activity": snapshot}))


def _fold_task_activity_cancel_requested(
    projection: InvocationProjection,
    event: TaskActivityCancelRequested,
    seq: int,
) -> InvocationProjection:
    activation, attempt = _attempt_for_activity(projection, event.activity_id, seq)
    _ensure_graph_open_if_known(projection, activation.graph_instance_id, seq)
    activity = attempt.activity
    if activity is None:
        _fail(seq, f"activity {event.activity_id!r} does not exist")
    if activity.state == "terminal_observed":
        _fail(seq, "cancel requested after terminal activity")
    if activity.cancel_requested:
        _fail(seq, "cancel transition is already durable")
    if attempt.status not in _LIVE_ATTEMPT_STATUSES:
        _fail(seq, "cancel requested without a matching active attempt")
    snapshot = _activity_snapshot(
        seq,
        {
            **activity.model_dump(mode="python"),
            "cancel_requested": True,
            "cancel_reason": event.reason,
        },
    )
    return _replace_latest_attempt(projection, activation, attempt.model_copy(update={"activity": snapshot}))


def _fold_task_activity_terminal_observed(
    projection: InvocationProjection,
    event: TaskActivityTerminalObserved,
    seq: int,
) -> InvocationProjection:
    activation, attempt = _attempt_for_activity(projection, event.activity_id, seq)
    _ensure_graph_open_if_known(projection, activation.graph_instance_id, seq)
    activity = attempt.activity
    if activity is None:
        _fail(seq, f"activity {event.activity_id!r} does not exist")
    if activity.state == "terminal_observed":
        _fail(seq, "terminal activity is already observed")
    if attempt.status not in _LIVE_ATTEMPT_STATUSES:
        _fail(seq, "terminal observed without a matching active attempt")
    if event.outcome.status == "succeeded":
        if activity.state != "bound":
            _fail(seq, "success requires a bound activity")
    else:
        if event.promotion_receipt_digest is not None:
            _fail(seq, "failed terminal activity cannot carry a promotion receipt")
        if attempt.prepared_commit is not None:
            _fail(seq, "failed terminal activity cannot prepare promotion")
        if activity.state == "dispatch_started" and event.terminal_proof_digest is None:
            _fail(seq, "unbound terminal after dispatch_started requires terminal_proof_digest")
        if activity.state not in {"prepared", "dispatch_started", "bound"}:
            _fail(seq, "terminal observed from an illegal activity state")
    snapshot = _activity_snapshot(
        seq,
        {
            **activity.model_dump(mode="python"),
            "state": "terminal_observed",
            "terminal": event.outcome,
            "outcome_digest": event.outcome_digest,
            "terminal_proof_digest": event.terminal_proof_digest,
            "staged_write_set_digest": event.staged_write_set_digest,
            "promotion_receipt_digest": event.promotion_receipt_digest,
        },
    )
    return _replace_latest_attempt(projection, activation, attempt.model_copy(update={"activity": snapshot}))


def _fold_task_lease_adopted(
    projection: InvocationProjection,
    event: TaskLeaseAdopted,
    seq: int,
) -> InvocationProjection:
    found = _find_attempt_for_activity(projection, event.activity_id)
    if found is None or not event.reconciliation_evidence_digest:
        _fail(seq, "lease adoption requires reconciliation evidence")
    activation, attempt = found
    _ensure_graph_open_if_known(projection, activation.graph_instance_id, seq)
    if (
        event.activation_id != activation.activation_id
        or event.attempt != attempt.attempt
        or attempt.lease_owner_id is None
        or (attempt.lease_task_id is not None and attempt.lease_task_id != event.task_id)
    ):
        _fail(seq, "lease adoption does not match the active attempt")
    if attempt.status not in _LIVE_ATTEMPT_STATUSES:
        _fail(seq, "lease adoption without a matching active attempt")
    if attempt.lease_heartbeat_at is not None and event.heartbeat_at < attempt.lease_heartbeat_at:
        _fail(seq, "lease adoption timestamps are inconsistent")
    attempt = attempt.model_copy(
        update={
            "lease_owner_id": event.owner_id,
            "lease_task_id": event.task_id,
            "lease_acquired_at": event.acquired_at,
            "lease_heartbeat_at": event.heartbeat_at,
            "lease_expires_at_value": event.expires_at,
        }
    )
    return _replace_latest_attempt(projection, activation, attempt)


def _activity_snapshot(seq: int, payload: dict[str, object]) -> TaskActivitySnapshot:
    try:
        return TaskActivitySnapshot.model_validate(payload)
    except ValueError as error:
        _fail(seq, str(error))


def _find_attempt_for_activity(
    projection: InvocationProjection, activity_id: str
) -> tuple[ActivationRecord, AttemptRecord] | None:
    matches = tuple(
        (activation, attempt)
        for activation in projection.activations
        for attempt in activation.attempts
        if attempt.activity is not None and attempt.activity.activity_id == activity_id
    )
    if len(matches) != 1:
        return None
    return matches[0]


def _attempt_for_activity(
    projection: InvocationProjection, activity_id: str, seq: int
) -> tuple[ActivationRecord, AttemptRecord]:
    found = _find_attempt_for_activity(projection, activity_id)
    if found is None:
        _fail(seq, f"activity {activity_id!r} does not exist")
    return found


def _replace_latest_attempt(
    projection: InvocationProjection,
    activation: ActivationRecord,
    attempt: AttemptRecord,
) -> InvocationProjection:
    attempts = tuple(attempt if item.attempt == attempt.attempt else item for item in activation.attempts)
    return _replace_activation(projection, activation.model_copy(update={"attempts": attempts}))


def _all_effect_receipts_present(
    projection: InvocationProjection,
    activation_id: str,
    attempt: AttemptRecord,
) -> bool:
    prepared = attempt.prepared_commit
    if prepared is None:
        return False
    records = tuple(
        item
        for item in projection.effects
        if item.activation_id == activation_id and item.task_attempt == attempt.attempt
    )
    if len(records) != len(prepared.effect_ids):
        return False
    return all(item.status == "applied" for item in records)


def _fail_pending_effect(
    projection: InvocationProjection,
    activation: ActivationRecord,
    attempt: AttemptRecord,
    failure: TaskFailure,
    seq: int,
) -> InvocationProjection:
    remaining_ids = {
        item.effect_id
        for item in projection.effects
        if item.activation_id == activation.activation_id
        and item.task_attempt == attempt.attempt
        and item.status in {"committed", "applying"}
    }
    if not remaining_ids:
        _fail(seq, "effect failure without a pending effect")
    attempt = attempt.model_copy(update={"status": "failed", "failure": failure})
    activation = activation.model_copy(update={"attempts": (*activation.attempts[:-1], attempt)})
    effects = tuple(
        item.model_copy(update={"status": "permanently_failed", "failure": failure})
        if item.effect_id in remaining_ids
        else item
        for item in projection.effects
    )
    return _replace_activation(projection.model_copy(update={"effects": effects}), activation)


def _effect_idempotency_key(
    lock_digest: str | None,
    effect_id: str,
    kind: str,
    payload: FrozenJSONValue,
) -> str:
    if lock_digest is None:
        raise ProjectionError("started projection requires complete invocation identity")
    return canonical_digest(
        {
            "lock_digest": lock_digest,
            "effect_id": effect_id,
            "kind": kind,
            "payload_digest": canonical_digest(cast(JSONValue, thaw_json(payload))),
        }
    )


def _effect(projection: InvocationProjection, effect_id: str, seq: int) -> EffectRecord:
    effect = next((item for item in projection.effects if item.effect_id == effect_id), None)
    if effect is None:
        _fail(seq, f"effect {effect_id!r} was not committed")
    return effect


def _replace_effect(projection: InvocationProjection, replacement: EffectRecord) -> InvocationProjection:
    effects = tuple(
        replacement if item.effect_id == replacement.effect_id else item for item in projection.effects
    )
    return projection.model_copy(update={"effects": effects})


def _require_no_pending_effects(
    projection: InvocationProjection,
    seq: int,
    graph_instance_id: str | None,
) -> None:
    pending_activations = any(
        item.attempts
        and item.attempts[-1].status == "effect_pending"
        and (graph_instance_id is None or item.graph_instance_id == graph_instance_id)
        for item in projection.activations
    )
    pending_effects = False
    for item in projection.effects:
        if item.status not in {"committed", "applying"}:
            continue
        if graph_instance_id is None:
            pending_effects = True
            break
        activation = _find_activation(projection, item.activation_id)
        if activation is not None and activation.graph_instance_id == graph_instance_id:
            pending_effects = True
            break
    if pending_activations or pending_effects:
        _fail(seq, "graph terminal while effects remain pending")


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
    if any(
        item.attempts and item.attempts[-1].status in _LIVE_ATTEMPT_STATUSES
        for item in projection.activations
    ):
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
    "ActivityRecoveryDecision",
    "activity_id_for_attempt",
    "attempt_directory_id",
    "attempt_identity_digest",
    "AttemptRecord",
    "AttemptStatus",
    "CommitResult",
    "EffectRecord",
    "FoldCursor",
    "GraphInstanceRecord",
    "InvocationProjection",
    "PendingInterrupt",
    "PlannedTask",
    "PlanResult",
    "PreparedTaskCommit",
    "ProjectionError",
    "ReconcileStatus",
    "RecoveryDecisionKind",
    "RecoveryResult",
    "TokenRecord",
    "ValidationReceipt",
    "fold_events",
]
