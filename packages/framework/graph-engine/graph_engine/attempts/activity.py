from __future__ import annotations

import asyncio
import concurrent.futures
import json
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from typing import cast

from graph_engine.canonical import JSONValue, canonical_digest, canonical_json_bytes
from graph_engine.errors import GraphEngineError
from graph_engine.plugin_api import TaskActivitySnapshot, TaskOutcome, TaskWorkspaceIdentity
from graph_engine.attempts.events import (
    ActivityBound as JournalActivityBound,
    ActivityDispatchStarted as JournalActivityDispatchStarted,
    AttemptSnapshot,
)
from graph_engine.attempts.keys import AttemptKey
from graph_engine.persistence.attempt_journal import AttemptJournalIntegrityError, AttemptJournalPort
from graph_engine.persistence.runner_lease import StaleFencingToken
from graph_engine.evidence.events import (
    EffectIntentCommitted,
    EventEnvelope,
    GraphStarted,
    InvocationStarted,
    NodeActivated,
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
    TaskPromotionCompleted,
    TokenConsumed,
    TokenOffered,
)
from graph_engine.attempts.host_protocol import (
    TASK_HOST_WIRE_SCHEMA_VERSION,
    TaskActivityRpcIdentity,
    TaskHostCancelCall,
    TaskHostExecuteCall,
    TaskHostReconcileCall,
)
from graph_engine.composition.lock import pinned_execution_host_lock
from graph_engine.evidence.ledger import (
    Ledger,
    LedgerConflictError,
    LedgerError,
    LedgerPublicationIndeterminate,
    append_validated_batch,
)
from graph_engine.evidence.checkpoint import write_checkpoint
from graph_engine.evidence.models import (
    FoldCursor,
    InvocationProjection,
    PlannedTask,
    ProjectionError,
    ReconcileStatus,
    RecoveryDecisionKind,
    fold_events,
)


MAX_ACTIVITY_VALUE_BYTES = 16 * 1024
_LIVE_ATTEMPT_STATUSES = {"running", "promotion_pending", "effect_pending"}
_RECOVERY_DECISIONS: dict[ReconcileStatus, RecoveryDecisionKind] = {
    "not_dispatched": "execute_same_attempt",
    "running": "adopt_same_attempt",
    "terminal": "promote_same_attempt",
    "absent": "finalize_failure_then_retry_policy",
    "indeterminate": "block",
}


def recovery_decision_for_status(status: ReconcileStatus) -> RecoveryDecisionKind:
    return _RECOVERY_DECISIONS[status]


def attempt_activity_in_flight(state: str | None) -> bool:
    return state in {"prepared", "dispatch_started", "bound"}


def attempt_activity_is_terminal(state: str | None) -> bool:
    return state == "terminal_observed"


class TaskActivityConflict(GraphEngineError):
    """Raised for an illegal or competing activity transition."""


class TaskActivityIndeterminate(GraphEngineError):
    """Raised when an external activity outcome cannot be proven."""


class TaskActivityReferenceInvalid(GraphEngineError):
    """Raised when a reference is non-canonical, oversized, or changed."""


class TaskActivityRecoveryUnsupported(GraphEngineError):
    """Raised when a running activity lacks the required recoverable protocol."""


class AttemptWorkspaceLost(GraphEngineError):
    """Raised when the prepared attempt workspace cannot be authenticated."""


class TaskActivityProtocolViolation(GraphEngineError):
    """Raised when a handler result violates the closed activity contract."""


@dataclass(frozen=True, slots=True)
class BoundedCanonicalJson:
    value: JSONValue
    digest: str


def bounded_canonical_json(value: JSONValue, *, limit: int) -> BoundedCanonicalJson:
    if value is None:
        raise TaskActivityReferenceInvalid("activity value must not be JSON null")
    try:
        encoded = canonical_json_bytes(value)
    except (TypeError, ValueError) as error:
        raise TaskActivityReferenceInvalid("activity value is not canonical JSON") from error
    if len(encoded) > limit:
        raise TaskActivityReferenceInvalid("activity value exceeds the size bound")
    decoded = cast(JSONValue, json.loads(encoded.decode("utf-8")))
    return BoundedCanonicalJson(value=decoded, digest=canonical_digest(decoded))


class LedgerTaskActivityPort:
    """Engine-owned CAS port bound to one activity identity and ledger position."""

    __slots__ = ("_ledger", "_identity", "_transition_guard")

    def __init__(
        self,
        *,
        ledger: Ledger,
        identity: TaskActivityRpcIdentity,
        transition_guard: Callable[[], None] | None = None,
    ) -> None:
        if identity.activity_id is None:
            raise TaskActivityConflict("activity port requires an activity id")
        self._ledger = ledger
        self._identity = identity
        self._transition_guard = transition_guard

    @property
    def activity_id(self) -> str:
        activity_id = self._identity.activity_id
        if activity_id is None:
            raise TaskActivityConflict("activity port requires an activity id")
        return activity_id

    @property
    def snapshot(self) -> TaskActivitySnapshot:
        return self._authenticate_current()

    def mark_dispatch_started(self, fingerprint: JSONValue) -> TaskActivitySnapshot:
        canonical = bounded_canonical_json(fingerprint, limit=MAX_ACTIVITY_VALUE_BYTES)
        return self._append_or_authenticate(
            TaskActivityDispatchStarted(
                activity_id=self.activity_id,
                ordinal=1,
                dispatch_fingerprint=canonical.value,
                dispatch_fingerprint_digest=canonical.digest,
            ),
            drift_error=TaskActivityConflict,
        )

    def bind(self, reference: JSONValue) -> TaskActivitySnapshot:
        canonical = bounded_canonical_json(reference, limit=MAX_ACTIVITY_VALUE_BYTES)
        return self._append_or_authenticate(
            TaskActivityBound(
                activity_id=self.activity_id,
                reference=canonical.value,
                reference_digest=canonical.digest,
            ),
            drift_error=TaskActivityReferenceInvalid,
        )

    def _authenticate_current(self) -> TaskActivitySnapshot:
        self._guard()
        try:
            projection = fold_events(self._ledger.read_all())
        except ProjectionError as error:
            raise TaskActivityConflict("persisted activity cannot be authenticated") from error
        except LedgerError as error:
            raise TaskActivityIndeterminate("activity ledger cannot be authenticated") from error
        activity_id = self.activity_id
        if projection.invocation_id != self._identity.invocation_id:
            raise TaskActivityConflict("activity identity does not match the invocation")
        matches = tuple(
            (activation, attempt)
            for activation in projection.activations
            if activation.activation_id == self._identity.activation_id
            for attempt in activation.attempts
            if attempt.activity is not None and attempt.activity.activity_id == activity_id
        )
        if len(matches) != 1:
            raise TaskActivityConflict("activity identity does not match the invocation")
        activation, attempt = matches[0]
        activity = attempt.activity
        if activity is None:
            raise TaskActivityConflict("activity identity does not match the invocation")
        if (
            attempt.attempt != self._identity.attempt
            or attempt.status not in _LIVE_ATTEMPT_STATUSES
            or activation.status != "active"
            or (attempt.lease_task_id is not None and attempt.lease_task_id != self._identity.task_id)
        ):
            raise TaskActivityConflict("activity identity does not match the live attempt")
        return activity

    def _append_or_authenticate(
        self,
        event: RuntimeEvent,
        *,
        drift_error: type[GraphEngineError],
        remaining_retries: int = 1,
    ) -> TaskActivitySnapshot:
        snapshot = self._authenticate_current()
        matched = _existing_payload_match(snapshot, event)
        if matched is True:
            return snapshot
        if matched is False:
            raise drift_error(_drift_message(event))
        if not _transition_allowed(snapshot, event):
            raise TaskActivityConflict("activity cannot apply the requested transition")

        envelopes = self._ledger.read_all()
        expected_next_seq = envelopes[-1].seq + 1 if envelopes else 1
        expected = (EventEnvelope.from_event(expected_next_seq, event),)
        self._guard()
        try:
            append_validated_batch(
                self._ledger,
                (event,),
                expected_next_seq=expected_next_seq,
            )
        except (LedgerConflictError, LedgerPublicationIndeterminate) as error:
            return self._reconcile_append(
                event,
                expected,
                expected_next_seq=expected_next_seq,
                error=error,
                drift_error=drift_error,
                remaining_retries=(
                    0 if isinstance(error, LedgerPublicationIndeterminate) else remaining_retries
                ),
            )
        except ProjectionError as error:
            raise TaskActivityConflict("activity cannot apply the requested transition") from error
        self._guard()
        return self._authenticate_current()

    def _reconcile_append(
        self,
        event: RuntimeEvent,
        expected: tuple[EventEnvelope, ...],
        *,
        expected_next_seq: int,
        error: BaseException,
        drift_error: type[GraphEngineError],
        remaining_retries: int,
    ) -> TaskActivitySnapshot:
        try:
            persisted = self._ledger.read_all()
        except BaseException as read_error:
            raise TaskActivityIndeterminate("ledger publication outcome is indeterminate") from read_error
        offset = expected_next_seq - 1
        window = persisted[offset : offset + len(expected)]
        if window == expected:
            try:
                self._ledger.ensure_durable()
            except BaseException as durability_error:
                raise TaskActivityIndeterminate(
                    "ledger range is visible but its directory durability is indeterminate"
                ) from durability_error
            self._guard()
            return self._authenticate_current()
        if isinstance(error, LedgerPublicationIndeterminate):
            raise TaskActivityIndeterminate("ledger publication outcome is indeterminate") from error
        snapshot = self._authenticate_current()
        matched = _existing_payload_match(snapshot, event)
        if matched is True:
            return snapshot
        if matched is False:
            raise drift_error(_drift_message(event)) from error
        if remaining_retries > 0 and _transition_allowed(snapshot, event):
            return self._append_or_authenticate(
                event,
                drift_error=drift_error,
                remaining_retries=remaining_retries - 1,
            )
        raise TaskActivityConflict("activity identity or CAS position changed") from error

    def _guard(self) -> None:
        if self._transition_guard is not None:
            self._transition_guard()


def _existing_payload_match(snapshot: TaskActivitySnapshot, event: RuntimeEvent) -> bool | None:
    if isinstance(event, TaskActivityDispatchStarted):
        if snapshot.dispatch_fingerprint_digest is None:
            return None
        return snapshot.dispatch_fingerprint_digest == event.dispatch_fingerprint_digest
    if isinstance(event, TaskActivityBound):
        if snapshot.reference_digest is None:
            return None
        return snapshot.reference_digest == event.reference_digest
    raise TypeError(f"unsupported activity event: {type(event).__name__}")


def _transition_allowed(snapshot: TaskActivitySnapshot, event: RuntimeEvent) -> bool:
    if isinstance(event, TaskActivityDispatchStarted):
        return snapshot.state == "prepared" and snapshot.dispatch_fingerprint_digest is None
    if isinstance(event, TaskActivityBound):
        return snapshot.state == "dispatch_started" and snapshot.reference_digest is None
    return False


def _drift_message(event: RuntimeEvent) -> str:
    if isinstance(event, TaskActivityDispatchStarted):
        return "dispatch fingerprint drifted from the durable activity"
    return "activity reference changed after bind"


class JournalBackedTaskActivityPort:
    """Synchronous worker-facing port that commits activity mutations to one Attempt journal."""

    __slots__ = (
        "_journal",
        "_attempt_key",
        "_identity",
        "_workspace_identity",
        "_assert_live_fence",
        "_owner_loop",
        "_remaining_deadline",
        "_expected_request_digest",
        "_expected_product_lock_digest",
        "_expected_handler_id",
    )

    def __init__(
        self,
        *,
        journal: AttemptJournalPort,
        attempt_key: AttemptKey,
        identity: TaskActivityRpcIdentity,
        workspace_identity: TaskWorkspaceIdentity,
        assert_live_fence: Callable[[], Awaitable[None]],
        owner_loop: asyncio.AbstractEventLoop,
        remaining_deadline: float,
        expected_request_digest: str | None = None,
        expected_product_lock_digest: str | None = None,
        expected_handler_id: str | None = None,
    ) -> None:
        if identity.activity_id is None:
            raise TaskActivityConflict("activity port requires an activity id")
        if identity.wire_schema_version != TASK_HOST_WIRE_SCHEMA_VERSION:
            raise TaskActivityConflict("activity rpc identity version is not current")
        if remaining_deadline <= 0:
            raise TaskActivityIndeterminate("activity journal deadline elapsed")
        self._journal = journal
        self._attempt_key = attempt_key
        self._identity = identity
        self._workspace_identity = workspace_identity
        self._assert_live_fence = assert_live_fence
        self._owner_loop = owner_loop
        self._remaining_deadline = remaining_deadline
        self._expected_request_digest = expected_request_digest or identity.request_digest
        self._expected_product_lock_digest = expected_product_lock_digest or identity.product_lock_digest
        self._expected_handler_id = expected_handler_id or identity.handler_id

    @property
    def activity_id(self) -> str:
        activity_id = self._identity.activity_id
        if activity_id is None:
            raise TaskActivityConflict("activity port requires an activity id")
        return activity_id

    @property
    def snapshot(self) -> TaskActivitySnapshot:
        return self._submit(self._load_snapshot())

    def mark_dispatch_started(self, fingerprint: JSONValue) -> TaskActivitySnapshot:
        canonical = bounded_canonical_json(fingerprint, limit=MAX_ACTIVITY_VALUE_BYTES)
        return self._submit(
            self._mutate(
                JournalActivityDispatchStarted(
                    activity_id=self.activity_id,
                    dispatch_fingerprint=canonical.value,
                    dispatch_fingerprint_digest=canonical.digest,
                )
            )
        )

    def bind(self, reference: JSONValue) -> TaskActivitySnapshot:
        canonical = bounded_canonical_json(reference, limit=MAX_ACTIVITY_VALUE_BYTES)
        return self._submit(
            self._mutate(
                JournalActivityBound(
                    activity_id=self.activity_id,
                    reference=canonical.value,
                    reference_digest=canonical.digest,
                )
            )
        )

    def _submit(self, coroutine: Coroutine[object, object, TaskActivitySnapshot]) -> TaskActivitySnapshot:
        future = asyncio.run_coroutine_threadsafe(coroutine, self._owner_loop)
        try:
            return future.result(timeout=self._remaining_deadline)
        except TimeoutError as error:
            raise TaskActivityIndeterminate("activity journal commit timed out") from error
        except concurrent.futures.CancelledError as error:
            raise TaskActivityIndeterminate("activity journal commit was cancelled") from error

    async def _load_snapshot(self) -> TaskActivitySnapshot:
        await self._assert_live_fence()
        snapshot = await self._journal.load(self._attempt_key)
        self._authenticate_snapshot(snapshot)
        assert snapshot is not None
        return self._to_task_snapshot(snapshot)

    async def _mutate(
        self,
        event: JournalActivityDispatchStarted | JournalActivityBound,
    ) -> TaskActivitySnapshot:
        await self._assert_live_fence()
        snapshot = await self._journal.load(self._attempt_key)
        self._authenticate_snapshot(snapshot)
        assert snapshot is not None
        matched = _journal_payload_match(snapshot, event)
        if matched is True:
            return self._to_task_snapshot(snapshot)
        if matched is False:
            raise (
                TaskActivityConflict("dispatch fingerprint drifted from the durable activity")
                if isinstance(event, JournalActivityDispatchStarted)
                else TaskActivityReferenceInvalid("activity reference changed after bind")
            )
        if not _journal_transition_allowed(snapshot, event):
            raise TaskActivityConflict("activity cannot apply the requested transition")
        try:
            updated = await self._journal.append(
                self._attempt_key,
                (event,),
                expected_revision=snapshot.revision,
                fencing_token=self._identity.fencing_token,
            )
        except StaleFencingToken:
            raise
        except AttemptJournalIntegrityError as error:
            latest = await self._journal.load(self._attempt_key)
            self._authenticate_snapshot(latest)
            assert latest is not None
            matched = _journal_payload_match(latest, event)
            if matched is True:
                await self._journal.ensure_durable(self._attempt_key)
                return self._to_task_snapshot(latest)
            if matched is False:
                raise (
                    TaskActivityConflict("dispatch fingerprint drifted from the durable activity")
                    if isinstance(event, JournalActivityDispatchStarted)
                    else TaskActivityReferenceInvalid("activity reference changed after bind")
                ) from error
            raise TaskActivityConflict("activity identity or CAS position changed") from error
        await self._journal.ensure_durable(self._attempt_key)
        await self._assert_live_fence()
        return self._to_task_snapshot(updated)

    def _authenticate_snapshot(self, snapshot: AttemptSnapshot | None) -> None:
        if snapshot is None:
            raise TaskActivityConflict("activity identity does not match the live attempt")
        identity = self._identity
        if self._attempt_key.digest != identity.attempt_key_digest:
            raise TaskActivityConflict("attempt key digest mismatch")
        if snapshot.attempt_key.digest != identity.attempt_key_digest:
            raise TaskActivityConflict("attempt key digest mismatch")
        if snapshot.authorization_id != identity.authorization_id:
            raise TaskActivityConflict("authorization id mismatch")
        if snapshot.fencing_token != identity.fencing_token:
            if snapshot.fencing_token > identity.fencing_token:
                raise StaleFencingToken("fencing token is stale")
            raise TaskActivityConflict("fencing token mismatch")
        if identity.phase != "runtime":
            raise TaskActivityConflict("activity phase mismatch")
        if self._workspace_identity.identity_digest != identity.workspace_identity_digest:
            raise TaskActivityConflict("workspace identity mismatch")
        if identity.request_digest != self._expected_request_digest:
            raise TaskActivityConflict("request digest mismatch")
        if snapshot.graph_revision != identity.graph_revision:
            raise TaskActivityConflict("graph revision mismatch")
        if identity.product_lock_digest != self._expected_product_lock_digest:
            raise TaskActivityConflict("product lock mismatch")
        if identity.handler_id != self._expected_handler_id:
            raise TaskActivityConflict("handler identity mismatch")
        pinned = pinned_execution_host_lock()
        if (
            identity.host_implementation_id != pinned.implementation_id
            or identity.host_implementation_digest != pinned.implementation_digest
        ):
            raise TaskActivityConflict("host implementation mismatch")
        if snapshot.invocation_id != identity.invocation_id:
            raise TaskActivityConflict("activity identity does not match the invocation")
        if snapshot.activity_id != identity.activity_id:
            raise TaskActivityConflict("activity identity does not match the invocation")
        if snapshot.terminal is not None or snapshot.released:
            raise TaskActivityConflict("activity identity does not match the live attempt")
        if snapshot.activity_state not in {"prepared", "dispatch_started", "bound"}:
            raise TaskActivityConflict("activity identity does not match the live attempt")

    def _to_task_snapshot(self, snapshot: AttemptSnapshot) -> TaskActivitySnapshot:
        state = snapshot.activity_state
        if state is None:
            raise TaskActivityConflict("activity identity does not match the live attempt")
        payload: dict[str, object] = {
            "activity_id": self.activity_id,
            "request_digest": self._identity.request_digest,
            "workspace_identity": self._workspace_identity,
            "state": state,
            "dispatch_fingerprint": snapshot.activity_dispatch_fingerprint,
            "dispatch_fingerprint_digest": snapshot.activity_dispatch_fingerprint_digest,
            "reference": snapshot.activity_reference,
            "reference_digest": snapshot.activity_reference_digest,
        }
        if state == "terminal_observed":
            if snapshot.activity_outcome is None:
                raise TaskActivityConflict("terminal activity requires a canonical outcome digest")
            outcome = TaskOutcome.model_validate(snapshot.activity_outcome)
            payload["terminal"] = outcome
            payload["outcome_digest"] = canonical_digest(outcome.model_dump(mode="json"))
            payload["staged_write_set_digest"] = snapshot.promotion_staged_digest or ("0" * 64)
        return TaskActivitySnapshot.model_validate(payload)


def journal_backed_activity_factory(
    *,
    journal: AttemptJournalPort,
    attempt_key: AttemptKey,
    owner_loop: asyncio.AbstractEventLoop,
    assert_live_fence: Callable[[], Awaitable[None]],
) -> Callable[
    [TaskHostExecuteCall | TaskHostReconcileCall | TaskHostCancelCall],
    JournalBackedTaskActivityPort,
]:
    def factory(
        call: TaskHostExecuteCall | TaskHostReconcileCall | TaskHostCancelCall,
        *,
        remaining_deadline: float,
    ) -> JournalBackedTaskActivityPort:
        return JournalBackedTaskActivityPort(
            journal=journal,
            attempt_key=attempt_key,
            identity=call.activity_rpc,
            workspace_identity=call.attempt_root.workspace_identity,
            assert_live_fence=assert_live_fence,
            owner_loop=owner_loop,
            remaining_deadline=remaining_deadline,
            expected_request_digest=canonical_digest(cast(JSONValue, call.request.model_dump(mode="json"))),
            expected_product_lock_digest=call.request.invocation.lock_digest,
            expected_handler_id=call.request.capability_id,
        )

    return factory  # type: ignore[return-value]


def _journal_payload_match(
    snapshot: AttemptSnapshot,
    event: JournalActivityDispatchStarted | JournalActivityBound,
) -> bool | None:
    if isinstance(event, JournalActivityDispatchStarted):
        if snapshot.activity_dispatch_fingerprint_digest is None:
            return None
        return snapshot.activity_dispatch_fingerprint_digest == event.dispatch_fingerprint_digest
    if snapshot.activity_reference_digest is None:
        return None
    return snapshot.activity_reference_digest == event.reference_digest


def _journal_transition_allowed(
    snapshot: AttemptSnapshot,
    event: JournalActivityDispatchStarted | JournalActivityBound,
) -> bool:
    if isinstance(event, JournalActivityDispatchStarted):
        return snapshot.activity_state == "prepared" and snapshot.activity_dispatch_fingerprint_digest is None
    return snapshot.activity_state == "dispatch_started" and snapshot.activity_reference_digest is None


__all__ = [
    "AttemptWorkspaceLost",
    "BoundedCanonicalJson",
    "EffectIntentCommitted",
    "EventEnvelope",
    "FoldCursor",
    "GraphStarted",
    "InvocationProjection",
    "InvocationStarted",
    "Ledger",
    "LedgerConflictError",
    "LedgerError",
    "LedgerPublicationIndeterminate",
    "JournalBackedTaskActivityPort",
    "LedgerTaskActivityPort",
    "MAX_ACTIVITY_VALUE_BYTES",
    "NodeActivated",
    "PlannedTask",
    "ProjectionError",
    "ReconcileStatus",
    "RecoveryDecisionKind",
    "RuntimeEvent",
    "TaskActivityBound",
    "TaskActivityCancelRequested",
    "TaskActivityConflict",
    "TaskActivityDispatchStarted",
    "TaskActivityIndeterminate",
    "TaskActivityPrepared",
    "TaskActivityProtocolViolation",
    "TaskActivityRecoveryUnsupported",
    "TaskActivityReferenceInvalid",
    "TaskActivityTerminalObserved",
    "TaskAttemptFailed",
    "TaskAttemptStarted",
    "TaskAttemptStopped",
    "TaskAttemptSucceeded",
    "TaskCommitPrepared",
    "TaskLeaseAcquired",
    "TaskLeaseAdopted",
    "TaskPromotionCompleted",
    "TokenConsumed",
    "TokenOffered",
    "append_validated_batch",
    "attempt_activity_in_flight",
    "attempt_activity_is_terminal",
    "bounded_canonical_json",
    "fold_events",
    "journal_backed_activity_factory",
    "recovery_decision_for_status",
    "write_checkpoint",
]
