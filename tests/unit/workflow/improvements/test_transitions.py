"""Tests for Improvement state transition matrix."""

from __future__ import annotations

import pytest

from assurance_agent.artifacts.models.improvements import ImprovementState
from assurance_agent.workflow.improvements.transitions import (
    ALLOWED_IMPROVEMENT_TRANSITIONS,
    InvalidImprovementTransitionError,
    assert_improvement_transition,
)


@pytest.mark.parametrize(
    ("source", "target"),
    [
        ("proposed", "approved"),
        ("approved", "evaluating"),
        ("evaluating", "awaiting_baseline"),
        ("applied", "rolled_back"),
        ("rolled_back", "needs_rework"),
    ],
)
def test_allowed_transitions(source: str, target: str) -> None:
    assert_improvement_transition(ImprovementState(source), ImprovementState(target))


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (ImprovementState.PROPOSED, ImprovementState.APPROVED),
        (ImprovementState.PROPOSED, ImprovementState.REJECTED),
        (ImprovementState.PROPOSED, ImprovementState.NEEDS_REWORK),
        (ImprovementState.PROPOSED, ImprovementState.SUPERSEDED),
        (ImprovementState.APPROVED, ImprovementState.EVALUATING),
        (ImprovementState.APPROVED, ImprovementState.EXPORTED),
        (ImprovementState.APPROVED, ImprovementState.SUPERSEDED),
        (ImprovementState.NEEDS_REWORK, ImprovementState.PROPOSED),
        (ImprovementState.NEEDS_REWORK, ImprovementState.SUPERSEDED),
        (ImprovementState.EVALUATING, ImprovementState.APPLIED),
        (ImprovementState.EVALUATING, ImprovementState.ROLLED_BACK),
        (ImprovementState.EVALUATING, ImprovementState.AWAITING_BASELINE),
        (ImprovementState.EVALUATING, ImprovementState.EVAL_ERROR),
        (ImprovementState.EVALUATING, ImprovementState.SUPERSEDED),
        (ImprovementState.AWAITING_BASELINE, ImprovementState.EVALUATING),
        (ImprovementState.AWAITING_BASELINE, ImprovementState.SUPERSEDED),
        (ImprovementState.EVAL_ERROR, ImprovementState.EVALUATING),
        (ImprovementState.EVAL_ERROR, ImprovementState.SUPERSEDED),
        (ImprovementState.EXPORTED, ImprovementState.APPLIED),
        (ImprovementState.EXPORTED, ImprovementState.NEEDS_REWORK),
        (ImprovementState.EXPORTED, ImprovementState.SUPERSEDED),
        (ImprovementState.APPLIED, ImprovementState.ROLLED_BACK),
        (ImprovementState.APPLIED, ImprovementState.SUPERSEDED),
        (ImprovementState.ROLLED_BACK, ImprovementState.NEEDS_REWORK),
        (ImprovementState.ROLLED_BACK, ImprovementState.SUPERSEDED),
    ],
)
def test_complete_allowed_matrix(source: ImprovementState, target: ImprovementState) -> None:
    assert_improvement_transition(source, target)
    assert target in ALLOWED_IMPROVEMENT_TRANSITIONS[source]


@pytest.mark.parametrize("state", list(ImprovementState))
def test_self_transitions_rejected(state: ImprovementState) -> None:
    with pytest.raises(InvalidImprovementTransitionError):
        assert_improvement_transition(state, state)


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (ImprovementState.REJECTED, ImprovementState.PROPOSED),
        (ImprovementState.SUPERSEDED, ImprovementState.APPROVED),
        (ImprovementState.PROPOSED, ImprovementState.EVALUATING),
        (ImprovementState.APPROVED, ImprovementState.APPLIED),
        (ImprovementState.APPLIED, ImprovementState.NEEDS_REWORK),
        (ImprovementState.NEEDS_REWORK, ImprovementState.APPROVED),
        (ImprovementState.AWAITING_BASELINE, ImprovementState.APPLIED),
        (ImprovementState.EVAL_ERROR, ImprovementState.APPLIED),
    ],
)
def test_illegal_transitions_rejected(source: ImprovementState, target: ImprovementState) -> None:
    with pytest.raises(InvalidImprovementTransitionError):
        assert_improvement_transition(source, target)


def test_terminal_states_have_no_outgoing() -> None:
    assert ALLOWED_IMPROVEMENT_TRANSITIONS[ImprovementState.REJECTED] == frozenset()
    assert ALLOWED_IMPROVEMENT_TRANSITIONS[ImprovementState.SUPERSEDED] == frozenset()
