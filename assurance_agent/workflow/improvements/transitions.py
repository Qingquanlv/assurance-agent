"""Pure Improvement lifecycle transition validation.

Rejects self-transitions. Duplicate-event idempotency is handled by the Ledger,
not this state machine.
"""

from __future__ import annotations

from assurance_agent.artifacts.models.improvements import ImprovementState
from assurance_agent.exceptions import AaError

# Exact graph including human-approved recovery edges (2026-07-26):
# needs_rework -> proposed, awaiting_baseline -> evaluating, eval_error -> evaluating.
_ALLOWED: dict[ImprovementState, frozenset[ImprovementState]] = {
    ImprovementState.PROPOSED: frozenset(
        {
            ImprovementState.APPROVED,
            ImprovementState.REJECTED,
            ImprovementState.NEEDS_REWORK,
            ImprovementState.SUPERSEDED,
        }
    ),
    ImprovementState.APPROVED: frozenset(
        {
            ImprovementState.EVALUATING,
            ImprovementState.EXPORTED,
            ImprovementState.REJECTED,
            ImprovementState.NEEDS_REWORK,
            ImprovementState.SUPERSEDED,
        }
    ),
    ImprovementState.NEEDS_REWORK: frozenset(
        {
            ImprovementState.PROPOSED,
            ImprovementState.SUPERSEDED,
        }
    ),
    ImprovementState.EVALUATING: frozenset(
        {
            ImprovementState.APPLIED,
            ImprovementState.ROLLED_BACK,
            ImprovementState.AWAITING_BASELINE,
            ImprovementState.EVAL_ERROR,
            ImprovementState.SUPERSEDED,
        }
    ),
    ImprovementState.AWAITING_BASELINE: frozenset(
        {
            ImprovementState.EVALUATING,
            ImprovementState.SUPERSEDED,
        }
    ),
    ImprovementState.EVAL_ERROR: frozenset(
        {
            ImprovementState.EVALUATING,
            ImprovementState.SUPERSEDED,
        }
    ),
    ImprovementState.EXPORTED: frozenset(
        {
            ImprovementState.APPLIED,
            ImprovementState.NEEDS_REWORK,
            ImprovementState.SUPERSEDED,
        }
    ),
    ImprovementState.APPLIED: frozenset(
        {
            ImprovementState.ROLLED_BACK,
            ImprovementState.SUPERSEDED,
        }
    ),
    ImprovementState.ROLLED_BACK: frozenset(
        {
            ImprovementState.NEEDS_REWORK,
            ImprovementState.SUPERSEDED,
        }
    ),
    ImprovementState.REJECTED: frozenset(),
    ImprovementState.SUPERSEDED: frozenset(),
}

ALLOWED_IMPROVEMENT_TRANSITIONS: dict[ImprovementState, frozenset[ImprovementState]] = _ALLOWED


class InvalidImprovementTransitionError(AaError):
    """Raised when an Improvement state transition is not in the allowed graph."""


def assert_improvement_transition(current: ImprovementState, target: ImprovementState) -> None:
    if current is target:
        raise InvalidImprovementTransitionError(f"self-transition is not allowed for state {current.value}")
    allowed = _ALLOWED.get(current, frozenset())
    if target not in allowed:
        raise InvalidImprovementTransitionError(
            f"transition {current.value} -> {target.value} is not allowed"
        )
