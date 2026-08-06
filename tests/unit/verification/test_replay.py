"""Deterministic counterexample replay (adversarial discovery Phase 1, Task B)."""

from __future__ import annotations

from dataclasses import dataclass, field

from assurance_agent.artifacts.models.discovery import (
    Counterexample,
    CounterexampleReplay,
    MinimizationInfo,
    OracleRule,
    OracleSpec,
    StatusCodeRule,
)
from assurance_agent.verification.oracle import OracleObservation, confirm_counterexample
from assurance_agent.verification.replay import (
    AttemptResult,
    ReplayAggregate,
    ReplayAttemptSpec,
    replay_counterexample,
)


def _hard_denied_500() -> OracleSpec:
    return OracleSpec(
        oracle_id="ORACLE-status",
        kind="hard_oracle",
        surface="api",
        rule=OracleRule(status_codes=StatusCodeRule(denied_codes=(500,))),
    )


def _ce(*, attempts: int = 3) -> Counterexample:
    return Counterexample(
        schema_version="1",
        counterexample_id="CE-001",
        campaign_id="CAM-001",
        round_id="R0001",
        surface="api",
        technique="stateful_fuzz",
        obligation_ids=(),
        oracle_id="ORACLE-status",
        oracle_kind="hard_oracle",
        environment_digest="sha256:" + ("a" * 64),
        generated_file_digests={},
        setup={"tenant": "A"},
        actions=({"method": "GET", "path": "/api/x"},),
        observed={"status_code": 500},
        expected={"status_code": 200},
        seed=12345,
        minimization=MinimizationInfo(status="minimized", parent_counterexample_id=None),
        replay=CounterexampleReplay(attempts=attempts, reproduced=0),
        finding_status="needs_review",
    )


@dataclass
class FakeRunner:
    """Deterministic injectable runner: returns a fixed sequence of AttemptResults."""

    results: list[AttemptResult]
    calls: list[ReplayAttemptSpec] = field(default_factory=list)
    _index: int = 0

    def run_attempt(self, spec: ReplayAttemptSpec) -> AttemptResult:
        self.calls.append(spec)
        if self._index >= len(self.results):
            raise AssertionError("FakeRunner exhausted")
        result = self.results[self._index]
        self._index += 1
        return result


def test_replay_three_of_three_violate_is_confirmable() -> None:
    oracle = _hard_denied_500()
    ce = _ce(attempts=3)
    runner = FakeRunner(
        results=[
            AttemptResult(observation=OracleObservation(status_code=500)),
            AttemptResult(observation=OracleObservation(status_code=500)),
            AttemptResult(observation=OracleObservation(status_code=500)),
        ]
    )
    aggregate = replay_counterexample(
        ce,
        oracle,
        runner,
        oracle_set_digest="digest-abc",
    )
    assert aggregate.attempts == 3
    assert aggregate.reproduced == 3
    assert aggregate.path == "confirmable"
    assert all(c.seed == ce.seed for c in runner.calls)
    assert all(c.oracle_set_digest == "digest-abc" for c in runner.calls)
    assert all(c.setup == ce.setup for c in runner.calls)
    assert all(c.actions == ce.actions for c in runner.calls)


def test_replay_two_of_three_not_confirmable() -> None:
    oracle = _hard_denied_500()
    ce = _ce(attempts=3)
    runner = FakeRunner(
        results=[
            AttemptResult(observation=OracleObservation(status_code=500)),
            AttemptResult(observation=OracleObservation(status_code=500)),
            AttemptResult(observation=OracleObservation(status_code=200)),
        ]
    )
    aggregate = replay_counterexample(ce, oracle, runner, oracle_set_digest="digest-abc")
    assert aggregate.attempts == 3
    assert aggregate.reproduced == 2
    assert aggregate.path == "not_reproduced"


def test_replay_environment_failure_inconclusive() -> None:
    oracle = _hard_denied_500()
    ce = _ce(attempts=3)
    runner = FakeRunner(
        results=[
            AttemptResult(observation=OracleObservation(status_code=500)),
            AttemptResult(outcome="environment_failure", detail="fixture missing"),
            AttemptResult(observation=OracleObservation(status_code=500)),
        ]
    )
    aggregate = replay_counterexample(ce, oracle, runner, oracle_set_digest="digest-abc")
    assert aggregate.path == "inconclusive_evidence"
    assert aggregate.reproduced < aggregate.attempts


def test_identical_fake_runner_sequences_are_deterministic() -> None:
    oracle = _hard_denied_500()
    ce = _ce(attempts=3)
    sequence = [
        AttemptResult(observation=OracleObservation(status_code=500)),
        AttemptResult(observation=OracleObservation(status_code=200)),
        AttemptResult(observation=OracleObservation(status_code=500)),
    ]
    a = replay_counterexample(ce, oracle, FakeRunner(results=list(sequence)), oracle_set_digest="digest-abc")
    b = replay_counterexample(ce, oracle, FakeRunner(results=list(sequence)), oracle_set_digest="digest-abc")
    assert a == b
    assert isinstance(a, ReplayAggregate)


def test_confirm_from_replay_aggregate_partial_not_confirmed() -> None:
    from assurance_agent.artifacts.models.discovery import OracleSetSnapshot

    oracle = _hard_denied_500()
    oracle_set = OracleSetSnapshot(
        schema_version="1",
        change_id="CH-1",
        campaign_id="CAM-001",
        oracles=(oracle,),
    )
    ce = _ce(attempts=3)
    attempts = (
        AttemptResult(observation=OracleObservation(status_code=500)),
        AttemptResult(observation=OracleObservation(status_code=500)),
        AttemptResult(observation=OracleObservation(status_code=200)),
    )
    updated = confirm_counterexample(ce, oracle_set, attempts)
    assert updated.finding_status != "confirmed"
    assert updated.replay.reproduced == 2
    assert updated.replay.attempts == 3
