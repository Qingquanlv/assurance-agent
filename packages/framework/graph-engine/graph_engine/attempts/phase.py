from __future__ import annotations

from enum import Enum

from graph_engine.attempts.events import AttemptSnapshot


class AttemptPhase(str, Enum):
    """Derived lifecycle phase for one durable Attempt snapshot."""

    NEW = "new"
    OPENED = "opened"
    AUTHORIZED = "authorized"
    ACTIVITY_ACTIVE = "activity_active"
    ACTIVITY_COMPLETED = "activity_completed"
    PREPARED = "prepared"
    PROMOTED = "promoted"
    TERMINATED = "terminated"
    RELEASED = "released"


class AttemptPhaseIntegrityError(ValueError):
    """Raised when a snapshot cannot map to one coherent Attempt phase."""


_ACTIVE_ACTIVITY_STATES = frozenset({"prepared", "dispatch_started", "bound"})
_KNOWN_ACTIVITY_STATES = _ACTIVE_ACTIVITY_STATES | {"terminal_observed"}


def derive_attempt_phase(snapshot: AttemptSnapshot) -> AttemptPhase:
    """Project the existing event-folded snapshot onto one explicit lifecycle phase.

    This is intentionally a pure projection: it persists no new state and changes
    no runtime behavior. Terminal/release states take precedence because failures
    may terminate an Attempt before prepare or promotion.
    """

    if snapshot.released:
        if snapshot.terminal is None:
            raise AttemptPhaseIntegrityError("released attempt is missing terminal state")
        return AttemptPhase.RELEASED

    if snapshot.terminal is not None:
        return AttemptPhase.TERMINATED

    promotion_fields = (
        snapshot.promotion_receipt_id,
        snapshot.promotion_receipt_digest,
        snapshot.promotion_staged_digest,
    )
    promotion_present = tuple(value is not None for value in promotion_fields)
    if any(promotion_present) and not all(promotion_present):
        raise AttemptPhaseIntegrityError("promotion receipt is only partially recorded")

    if all(promotion_present):
        return AttemptPhase.PROMOTED

    if snapshot.prepared_digest is not None:
        return AttemptPhase.PREPARED

    activity_state = snapshot.activity_state
    if activity_state is not None and activity_state not in _KNOWN_ACTIVITY_STATES:
        raise AttemptPhaseIntegrityError(f"unknown activity state: {activity_state}")

    if activity_state == "terminal_observed":
        return AttemptPhase.ACTIVITY_COMPLETED

    if activity_state in _ACTIVE_ACTIVITY_STATES:
        return AttemptPhase.ACTIVITY_ACTIVE

    if snapshot.authorization_id is not None:
        return AttemptPhase.AUTHORIZED

    if snapshot.contract_digest is not None:
        return AttemptPhase.OPENED

    return AttemptPhase.NEW


__all__ = [
    "AttemptPhase",
    "AttemptPhaseIntegrityError",
    "derive_attempt_phase",
]
