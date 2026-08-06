"""M3 Task 3: pure AttemptResult → ReplayAttemptReceipt mapping + digests."""

from __future__ import annotations

from assurance_agent.artifacts.models.discovery import (
    OracleRule,
    OracleSpec,
    StatusCodeRule,
)
from assurance_agent.artifacts.canonical import sha256_bytes
from assurance_agent.evidence.replay_telemetry import receipt_payload_digest
from assurance_agent.verification.oracle import OracleObservation
from assurance_agent.verification.replay import (
    AttemptResult,
    build_replay_attempt_receipts,
    replay_counterexample,
)
from tests.unit.verification.test_replay import FakeRunner, _ce, _hard_denied_500


def test_build_receipts_maps_outcomes() -> None:
    oracle = _hard_denied_500()
    results = (
        AttemptResult(observation=OracleObservation(status_code=500)),
        AttemptResult(observation=OracleObservation(status_code=200)),
        AttemptResult(outcome="environment_failure", detail="down"),
        AttemptResult(outcome="divergence", detail="drift"),
        AttemptResult(observation=None, outcome="ok", detail="missing"),
    )
    receipts = build_replay_attempt_receipts(
        counterexample_id="CE-001",
        seed=12345,
        base_revision="deadbeef",
        oracle_set_digest="digest-abc",
        oracle=oracle,
        attempt_results=results,
    )
    assert [r.outcome for r in receipts] == [
        "violate",
        "hold",
        "environment_failure",
        "divergence",
        "inconclusive",
    ]
    assert [r.attempt_index for r in receipts] == [0, 1, 2, 3, 4]
    assert all(r.base_revision == "deadbeef" for r in receipts)
    assert all(r.oracle_set_digest == "digest-abc" for r in receipts)
    assert all(r.seed == 12345 for r in receipts)


def test_same_seed_revision_oracle_digest_yields_identical_payload_digests() -> None:
    oracle = _hard_denied_500()
    sequence = [
        AttemptResult(observation=OracleObservation(status_code=500)),
        AttemptResult(observation=OracleObservation(status_code=500)),
        AttemptResult(observation=OracleObservation(status_code=500)),
    ]
    a = build_replay_attempt_receipts(
        counterexample_id="CE-001",
        seed=99,
        base_revision="rev-1",
        oracle_set_digest="osd-1",
        oracle=oracle,
        attempt_results=sequence,
        recorded_at="2026-08-05T00:00:00Z",
    )
    b = build_replay_attempt_receipts(
        counterexample_id="CE-001",
        seed=99,
        base_revision="rev-1",
        oracle_set_digest="osd-1",
        oracle=oracle,
        attempt_results=sequence,
        recorded_at="2026-08-05T99:99:99Z",
    )
    assert [receipt_payload_digest(r) for r in a] == [receipt_payload_digest(r) for r in b]


def test_replay_counterexample_exposes_attempt_results() -> None:
    oracle = _hard_denied_500()
    ce = _ce(attempts=2)
    runner = FakeRunner(
        results=[
            AttemptResult(observation=OracleObservation(status_code=500)),
            AttemptResult(observation=OracleObservation(status_code=500)),
        ]
    )
    aggregate = replay_counterexample(ce, oracle, runner, oracle_set_digest="d")
    assert len(aggregate.attempt_results) == 2
    assert all(r.observation is not None for r in aggregate.attempt_results)


def test_observed_digest_is_stable_for_same_observation() -> None:
    oracle = OracleSpec(
        oracle_id="ORACLE-status",
        kind="hard_oracle",
        surface="api",
        rule=OracleRule(status_codes=StatusCodeRule(denied_codes=(500,))),
    )
    obs = OracleObservation(status_code=500, claims={"tenant": "A"})
    receipts = build_replay_attempt_receipts(
        counterexample_id="CE-x",
        seed=1,
        base_revision="r",
        oracle_set_digest="d",
        oracle=oracle,
        attempt_results=(AttemptResult(observation=obs), AttemptResult(observation=obs)),
    )
    assert receipts[0].observed_digest == receipts[1].observed_digest
    assert receipts[0].observed_digest.startswith("sha256:")
    # Sanity: digest is content-addressed, not a random token.
    assert len(receipts[0].observed_digest) == len(sha256_bytes(b"x"))
