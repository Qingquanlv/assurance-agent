"""M3 Task 3: pure seed-replay rate + API adversarial expansion gate."""

from __future__ import annotations

from assurance_agent.artifacts.models.discovery import ReplayAttemptReceipt
from assurance_agent.evidence.replay_telemetry import (
    DEFAULT_API_C3_MIN_RATE,
    api_adversarial_expansion_allowed,
    compute_seed_replay_rate,
    receipt_payload_digest,
)


def _receipt(
    *,
    outcome: str = "violate",
    attempt_index: int = 0,
    recorded_at: str | None = None,
) -> ReplayAttemptReceipt:
    return ReplayAttemptReceipt(
        schema_version="1",
        counterexample_id="CE-001",
        seed=12345,
        attempt_index=attempt_index,
        base_revision="deadbeef",
        oracle_set_digest="sha256:" + ("a" * 64),
        outcome=outcome,  # type: ignore[arg-type]
        observed_digest="sha256:" + ("b" * 64),
        observed_status=500 if outcome == "violate" else 200,
        recorded_at=recorded_at,
    )


def test_compute_seed_replay_rate_empty_is_not_evaluated() -> None:
    success, attempts, rate = compute_seed_replay_rate(())
    assert success == 0
    assert attempts == 0
    assert rate is None


def test_compute_seed_replay_rate_counts_violate_as_success() -> None:
    receipts = (
        _receipt(outcome="violate", attempt_index=0),
        _receipt(outcome="hold", attempt_index=1),
        _receipt(outcome="violate", attempt_index=2),
    )
    success, attempts, rate = compute_seed_replay_rate(receipts)
    assert success == 2
    assert attempts == 3
    assert rate is not None
    assert abs(rate - (2 / 3)) < 1e-9


def test_compute_seed_replay_rate_full_success() -> None:
    receipts = tuple(_receipt(outcome="violate", attempt_index=i) for i in range(3))
    success, attempts, rate = compute_seed_replay_rate(receipts)
    assert (success, attempts, rate) == (3, 3, 1.0)


def test_compute_seed_replay_rate_env_and_divergence_are_not_success() -> None:
    receipts = (
        _receipt(outcome="environment_failure", attempt_index=0),
        _receipt(outcome="divergence", attempt_index=1),
        _receipt(outcome="inconclusive", attempt_index=2),
    )
    success, attempts, rate = compute_seed_replay_rate(receipts)
    assert success == 0
    assert attempts == 3
    assert rate == 0.0


def test_expansion_allowed_only_when_rate_meets_threshold() -> None:
    assert api_adversarial_expansion_allowed(1.0, DEFAULT_API_C3_MIN_RATE) is True
    assert api_adversarial_expansion_allowed(0.99, DEFAULT_API_C3_MIN_RATE) is False
    assert api_adversarial_expansion_allowed(None, DEFAULT_API_C3_MIN_RATE) is False
    assert api_adversarial_expansion_allowed(0.5, 0.5) is True
    assert api_adversarial_expansion_allowed(0.49, 0.5) is False


def test_receipt_payload_digest_ignores_recorded_at() -> None:
    a = _receipt(recorded_at=None)
    b = _receipt(recorded_at="2026-08-05T12:00:00Z")
    assert receipt_payload_digest(a) == receipt_payload_digest(b)


def test_receipt_payload_digest_changes_with_outcome() -> None:
    a = _receipt(outcome="violate")
    b = _receipt(outcome="hold")
    assert receipt_payload_digest(a) != receipt_payload_digest(b)
