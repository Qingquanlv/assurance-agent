from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

from graph_engine.canonical import JSONValue, canonical_digest, canonical_json_bytes
from graph_engine.errors import GraphEngineError
from graph_engine.plugin_api import TaskActivitySnapshot
from graph_engine.runtime.events import (
    EventEnvelope,
    RuntimeEvent,
    TaskActivityBound,
    TaskActivityDispatchStarted,
)
from graph_engine.attempts.host_protocol import TaskActivityRpcIdentity
from graph_engine.runtime.ledger import (
    Ledger,
    LedgerConflictError,
    LedgerError,
    LedgerPublicationIndeterminate,
    append_validated_batch,
)
from graph_engine.runtime.models import (
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


__all__ = [
    "AttemptWorkspaceLost",
    "BoundedCanonicalJson",
    "LedgerTaskActivityPort",
    "MAX_ACTIVITY_VALUE_BYTES",
    "TaskActivityConflict",
    "TaskActivityIndeterminate",
    "TaskActivityProtocolViolation",
    "TaskActivityRecoveryUnsupported",
    "TaskActivityReferenceInvalid",
    "attempt_activity_in_flight",
    "attempt_activity_is_terminal",
    "bounded_canonical_json",
    "recovery_decision_for_status",
]
