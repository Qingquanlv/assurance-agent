from __future__ import annotations

import pytest

from graph_engine.attempts.events import AttemptSnapshot, AttemptTerminated
from graph_engine.attempts.keys import AttemptKey
from graph_engine.attempts.phase import (
    AttemptPhase,
    AttemptPhaseIntegrityError,
    derive_attempt_phase,
)


_KEY = AttemptKey(digest="a" * 64)


def _snapshot(**updates: object) -> AttemptSnapshot:
    values: dict[str, object] = {
        "attempt_key": _KEY,
        "revision": 1,
        "fencing_token": 1,
    }
    values.update(updates)
    return AttemptSnapshot(**values)


@pytest.mark.parametrize(
    ("snapshot", "expected"),
    [
        (_snapshot(), AttemptPhase.NEW),
        (_snapshot(contract_digest="b" * 64), AttemptPhase.OPENED),
        (
            _snapshot(contract_digest="b" * 64, authorization_id="c" * 64),
            AttemptPhase.AUTHORIZED,
        ),
        (
            _snapshot(
                contract_digest="b" * 64,
                authorization_id="c" * 64,
                activity_state="prepared",
            ),
            AttemptPhase.ACTIVITY_ACTIVE,
        ),
        (
            _snapshot(
                contract_digest="b" * 64,
                authorization_id="c" * 64,
                activity_state="dispatch_started",
            ),
            AttemptPhase.ACTIVITY_ACTIVE,
        ),
        (
            _snapshot(
                contract_digest="b" * 64,
                authorization_id="c" * 64,
                activity_state="bound",
            ),
            AttemptPhase.ACTIVITY_ACTIVE,
        ),
        (
            _snapshot(
                contract_digest="b" * 64,
                authorization_id="c" * 64,
                activity_state="terminal_observed",
            ),
            AttemptPhase.ACTIVITY_COMPLETED,
        ),
        (
            _snapshot(
                contract_digest="b" * 64,
                authorization_id="c" * 64,
                activity_state="terminal_observed",
                prepared_digest="d" * 64,
            ),
            AttemptPhase.PREPARED,
        ),
        (
            _snapshot(
                contract_digest="b" * 64,
                authorization_id="c" * 64,
                activity_state="terminal_observed",
                prepared_digest="d" * 64,
                promotion_receipt_id="receipt-1",
                promotion_receipt_digest="e" * 64,
                promotion_staged_digest="f" * 64,
            ),
            AttemptPhase.PROMOTED,
        ),
    ],
)
def test_derive_attempt_phase(snapshot: AttemptSnapshot, expected: AttemptPhase) -> None:
    assert derive_attempt_phase(snapshot) is expected


def test_terminal_failure_takes_precedence_over_earlier_progress() -> None:
    snapshot = _snapshot(
        authorization_id="c" * 64,
        activity_state="terminal_observed",
        terminal=AttemptTerminated(
            resolution_kind="rejected",
            reason="policy rejected",
        ),
    )

    assert derive_attempt_phase(snapshot) is AttemptPhase.TERMINATED


def test_released_takes_precedence_over_terminal() -> None:
    snapshot = _snapshot(
        authorization_id="c" * 64,
        terminal=AttemptTerminated(
            resolution_kind="committed",
            receipt_id="receipt-1",
            receipt_digest="e" * 64,
        ),
        released=True,
    )

    assert derive_attempt_phase(snapshot) is AttemptPhase.RELEASED


def test_partial_promotion_receipt_is_rejected() -> None:
    snapshot = _snapshot(
        promotion_receipt_id="receipt-1",
        promotion_receipt_digest="e" * 64,
    )

    with pytest.raises(AttemptPhaseIntegrityError, match="partially recorded"):
        derive_attempt_phase(snapshot)


def test_released_without_terminal_is_rejected() -> None:
    with pytest.raises(AttemptPhaseIntegrityError, match="missing terminal"):
        derive_attempt_phase(_snapshot(released=True))


def test_unknown_activity_state_is_rejected() -> None:
    with pytest.raises(AttemptPhaseIntegrityError, match="unknown activity state"):
        derive_attempt_phase(_snapshot(activity_state="mystery"))
