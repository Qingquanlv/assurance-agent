"""Pure quarantine enter/release fold (M3 Task 4)."""

from __future__ import annotations

import pytest

from assurance_agent.artifacts.models.discovery import ReplayAttemptReceipt
from assurance_agent.artifacts.models.quarantine import (
    DEFAULT_RELEASE_CONSECUTIVE_SUCCESSES,
    QuarantineProjection,
)
from assurance_agent.evidence.quarantine import (
    active_subject_keys,
    apply_quarantine_event,
    consecutive_replay_successes,
    fold_receipts_into_projection,
    propose_enter_from_receipts,
    propose_enter_quarantine,
)


def _receipt(
    *,
    outcome: str = "violate",
    attempt_index: int = 0,
    counterexample_id: str = "CE-001",
) -> ReplayAttemptReceipt:
    return ReplayAttemptReceipt(
        schema_version="1",
        counterexample_id=counterexample_id,
        seed=1,
        attempt_index=attempt_index,
        base_revision="deadbeef",
        oracle_set_digest="sha256:" + ("a" * 64),
        outcome=outcome,  # type: ignore[arg-type]
        observed_digest="sha256:" + ("b" * 64),
    )


REF = "discovery/counterexamples/CE-001/replay/attempt-0.json"


def test_propose_enter_fails_closed_without_receipt_ref() -> None:
    assert (
        propose_enter_quarantine(
            subject_kind="property",
            subject_key="entities.dept.constraints.name_unique",
            reason="flaky",
            entered_at="2026-08-05T10:00:00Z",
            evidence_refs=(),
        )
        is None
    )


def test_propose_enter_from_receipts_only_when_flaky() -> None:
    flaky = (
        _receipt(outcome="violate", attempt_index=0),
        _receipt(outcome="hold", attempt_index=1),
    )
    entered = propose_enter_from_receipts(
        subject_kind="property",
        subject_key="entities.dept.constraints.name_unique",
        receipts=flaky,
        evidence_refs=(REF,),
        entered_at="2026-08-05T10:00:00Z",
    )
    assert entered is not None
    assert entered.status == "active"
    assert "seed_replay_rate=" in entered.reason

    stable = tuple(_receipt(outcome="violate", attempt_index=i) for i in range(2))
    assert (
        propose_enter_from_receipts(
            subject_kind="property",
            subject_key="entities.dept.constraints.name_unique",
            receipts=stable,
            evidence_refs=(REF,),
            entered_at="2026-08-05T10:00:00Z",
        )
        is None
    )


def test_release_requires_consecutive_success_policy() -> None:
    entered = propose_enter_quarantine(
        subject_kind="journey",
        subject_key="admin_creates_dept",
        reason="flaky",
        entered_at="2026-08-05T10:00:00Z",
        evidence_refs=(REF,),
        release_requires=DEFAULT_RELEASE_CONSECUTIVE_SUCCESSES,
    )
    assert entered is not None
    projection = QuarantineProjection(
        schema_version="1",
        change_id="CH-Q-001",
        entries=(entered,),
    )
    with pytest.raises(ValueError, match="consecutive"):
        apply_quarantine_event(
            projection,
            {
                "kind": "release",
                "subject_kind": "journey",
                "subject_key": "admin_creates_dept",
            },
        )

    after_one = apply_quarantine_event(
        projection,
        {
            "kind": "success",
            "subject_kind": "journey",
            "subject_key": "admin_creates_dept",
        },
    )
    assert after_one.entries[0].consecutive_successes == 1
    assert after_one.entries[0].status == "active"

    after_two = apply_quarantine_event(
        after_one,
        {
            "kind": "success",
            "subject_kind": "journey",
            "subject_key": "admin_creates_dept",
        },
    )
    released = apply_quarantine_event(
        after_two,
        {
            "kind": "release",
            "subject_kind": "journey",
            "subject_key": "admin_creates_dept",
        },
    )
    assert released.entries[0].status == "released"
    assert released.entries[0].consecutive_successes >= DEFAULT_RELEASE_CONSECUTIVE_SUCCESSES


def test_consecutive_replay_successes_trailing_violate_streak() -> None:
    receipts = (
        _receipt(outcome="hold", attempt_index=0),
        _receipt(outcome="violate", attempt_index=1),
        _receipt(outcome="violate", attempt_index=2),
    )
    assert consecutive_replay_successes(receipts) == 2


def test_multi_counterexample_receipts_cannot_prove_a_global_success_streak() -> None:
    entered = propose_enter_quarantine(
        subject_kind="property",
        subject_key="entities.dept.constraints.name_unique",
        reason="flaky",
        entered_at="2026-08-05T10:00:00Z",
        evidence_refs=(REF,),
    )
    assert entered is not None
    prior = QuarantineProjection(
        schema_version="1",
        change_id="CH-Q-ORDER",
        entries=(entered,),
    )
    receipts = (
        _receipt(counterexample_id="CE-A", outcome="hold", attempt_index=0),
        _receipt(counterexample_id="CE-B", outcome="violate", attempt_index=0),
        _receipt(counterexample_id="CE-B", outcome="violate", attempt_index=1),
    )

    projection = fold_receipts_into_projection(
        prior,
        change_id="CH-Q-ORDER",
        subject_receipts={("property", entered.subject_key): receipts},
        entered_at="2026-08-05T11:00:00Z",
    )

    assert projection.entries[0].status == "active"
    assert projection.entries[0].consecutive_successes == 0


def test_released_subject_preserves_success_count_for_unordered_new_ce_receipt() -> None:
    entered = propose_enter_quarantine(
        subject_kind="property",
        subject_key="entities.dept.constraints.name_unique",
        reason="flaky",
        entered_at="2026-08-05T10:00:00Z",
        evidence_refs=(
            "discovery/counterexamples/CE-A/replay/attempt-0.json",
            "discovery/counterexamples/CE-A/replay/attempt-1.json",
        ),
    )
    assert entered is not None
    released = entered.model_copy(
        update={
            "status": "released",
            "consecutive_successes": entered.release_requires,
        }
    )
    prior = QuarantineProjection(
        schema_version="1",
        change_id="CH-Q-RELEASED",
        entries=(released,),
    )
    receipts = (
        _receipt(counterexample_id="CE-A", attempt_index=0),
        _receipt(counterexample_id="CE-A", attempt_index=1),
        _receipt(counterexample_id="CE-B", attempt_index=0),
    )

    projection = fold_receipts_into_projection(
        prior,
        change_id="CH-Q-RELEASED",
        subject_receipts={("property", released.subject_key): receipts},
        entered_at="2026-08-05T11:00:00Z",
    )

    assert projection.entries[0].status == "released"
    assert projection.entries[0].consecutive_successes == released.consecutive_successes
    assert "discovery/counterexamples/CE-B/replay/attempt-0.json" in projection.entries[0].evidence_refs


def test_active_subject_keys_excludes_released() -> None:
    active = propose_enter_quarantine(
        subject_kind="property",
        subject_key="entities.dept.constraints.name_unique",
        reason="flaky",
        entered_at="2026-08-05T10:00:00Z",
        evidence_refs=(REF,),
    )
    assert active is not None
    released = active.model_copy(
        update={
            "status": "released",
            "subject_key": "entities.dept.constraints.other",
            "consecutive_successes": DEFAULT_RELEASE_CONSECUTIVE_SUCCESSES,
        }
    )
    projection = QuarantineProjection(
        schema_version="1",
        change_id="CH-Q-001",
        entries=(active, released),
    )
    assert active_subject_keys(projection) == frozenset({"entities.dept.constraints.name_unique"})
