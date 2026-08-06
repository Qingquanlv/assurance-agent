"""Frozen oracle evaluation (adversarial discovery Phase 1, Task B)."""

from __future__ import annotations

from assurance_agent.artifacts.models.discovery import (
    AuthIsolationRule,
    Counterexample,
    CounterexampleReplay,
    MinimizationInfo,
    OracleRule,
    OracleSetSnapshot,
    OracleSpec,
    StatusCodeRule,
)
from assurance_agent.verification.oracle import (
    OracleObservation,
    OracleVerdict,
    confirm_counterexample,
    evaluate_oracle,
)
from assurance_agent.verification.replay import AttemptResult


def _status_oracle(
    *,
    kind: str = "hard_oracle",
    allowed: tuple[int, ...] = (),
    denied: tuple[int, ...] = (),
    oracle_id: str = "ORACLE-status",
) -> OracleSpec:
    return OracleSpec(
        oracle_id=oracle_id,
        kind=kind,  # type: ignore[arg-type]
        surface="api",
        rule=OracleRule(status_codes=StatusCodeRule(allowed_codes=allowed, denied_codes=denied)),
    )


def _auth_oracle(
    *,
    mode: str = "deny_cross_tenant",
    kind: str = "hard_oracle",
    subject_claim: str | None = "tenant_id",
    resource_claim: str | None = "resource_tenant_id",
    oracle_id: str = "ORACLE-auth",
) -> OracleSpec:
    return OracleSpec(
        oracle_id=oracle_id,
        kind=kind,  # type: ignore[arg-type]
        surface="api",
        rule=OracleRule(
            auth_isolation=AuthIsolationRule(
                mode=mode,  # type: ignore[arg-type]
                subject_claim=subject_claim,
                resource_claim=resource_claim,
            )
        ),
    )


def _ce(**overrides: object) -> Counterexample:
    base: dict[str, object] = dict(
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
        setup={},
        actions=({"method": "GET", "path": "/api/x"},),
        observed={"status_code": 500},
        expected={"status_code": 200},
        seed=42,
        minimization=MinimizationInfo(status="raw", parent_counterexample_id=None),
        replay=CounterexampleReplay(attempts=0, reproduced=0),
        finding_status="needs_review",
    )
    base.update(overrides)
    return Counterexample(**base)  # type: ignore[arg-type]


def test_status_allowed_code_holds() -> None:
    oracle = _status_oracle(allowed=(200, 201))
    verdict = evaluate_oracle(oracle, OracleObservation(status_code=200))
    assert verdict == OracleVerdict(kind="hold", reason_code="allowed_status_code")


def test_status_denied_code_violates_hard_oracle() -> None:
    oracle = _status_oracle(denied=(500,))
    verdict = evaluate_oracle(oracle, OracleObservation(status_code=500))
    assert verdict == OracleVerdict(kind="violate", reason_code="denied_status_code")


def test_status_not_in_allowed_violates() -> None:
    oracle = _status_oracle(allowed=(200,))
    verdict = evaluate_oracle(oracle, OracleObservation(status_code=500))
    assert verdict == OracleVerdict(kind="violate", reason_code="status_not_allowed")


def test_status_not_denied_holds() -> None:
    oracle = _status_oracle(denied=(500,))
    verdict = evaluate_oracle(oracle, OracleObservation(status_code=200))
    assert verdict == OracleVerdict(kind="hold", reason_code="status_not_denied")


def test_missing_status_code_is_inconclusive() -> None:
    oracle = _status_oracle(allowed=(200,))
    verdict = evaluate_oracle(oracle, OracleObservation())
    assert verdict.kind == "inconclusive"
    assert verdict.reason_code == "missing_status_code"


def test_auth_isolation_deny_cross_tenant_hold_when_denied() -> None:
    oracle = _auth_oracle()
    verdict = evaluate_oracle(
        oracle,
        OracleObservation(
            status_code=403,
            claims={"tenant_id": "A", "resource_tenant_id": "B"},
        ),
    )
    assert verdict == OracleVerdict(kind="hold", reason_code="cross_tenant_denied")


def test_auth_isolation_deny_cross_tenant_violate_when_allowed() -> None:
    oracle = _auth_oracle()
    verdict = evaluate_oracle(
        oracle,
        OracleObservation(
            status_code=200,
            claims={"tenant_id": "A", "resource_tenant_id": "B"},
        ),
    )
    assert verdict == OracleVerdict(kind="violate", reason_code="cross_tenant_allowed")


def test_auth_isolation_missing_claims_inconclusive() -> None:
    oracle = _auth_oracle()
    verdict = evaluate_oracle(oracle, OracleObservation(status_code=200, claims={}))
    assert verdict.kind == "inconclusive"
    assert verdict.reason_code == "missing_claim_value"


def test_search_heuristic_never_returns_violate() -> None:
    oracle = _status_oracle(kind="search_heuristic", denied=(500,))
    verdict = evaluate_oracle(oracle, OracleObservation(status_code=500))
    assert verdict.kind == "heuristic_signal"
    assert verdict.kind != "violate"


def test_environment_oracle_missing_env_facts_inconclusive() -> None:
    oracle = _status_oracle(kind="environment_oracle", allowed=(200,))
    verdict = evaluate_oracle(oracle, OracleObservation(status_code=200))
    assert verdict == OracleVerdict(kind="inconclusive", reason_code="missing_env_facts")


def test_confirm_helper_rejects_search_heuristic_signal() -> None:
    oracle = _status_oracle(kind="search_heuristic", denied=(500,), oracle_id="ORACLE-heur")
    oracle_set = OracleSetSnapshot(
        schema_version="1",
        change_id="CH-1",
        campaign_id="CAM-001",
        oracles=(oracle,),
    )
    ce = _ce(oracle_id="ORACLE-heur", oracle_kind="search_heuristic")
    attempts = (
        AttemptResult(observation=OracleObservation(status_code=500)),
        AttemptResult(observation=OracleObservation(status_code=500)),
        AttemptResult(observation=OracleObservation(status_code=500)),
    )
    updated = confirm_counterexample(ce, oracle_set, attempts)
    assert updated.finding_status == "needs_review"
    assert updated.finding_status != "confirmed"
    assert updated.replay.attempts == 3
    assert updated.replay.reproduced == 0


def test_confirm_helper_confirms_hard_full_reproduce() -> None:
    oracle = _status_oracle(denied=(500,))
    oracle_set = OracleSetSnapshot(
        schema_version="1",
        change_id="CH-1",
        campaign_id="CAM-001",
        oracles=(oracle,),
    )
    ce = _ce()
    attempts = tuple(AttemptResult(observation=OracleObservation(status_code=500)) for _ in range(3))
    updated = confirm_counterexample(ce, oracle_set, attempts)
    assert updated.finding_status == "confirmed"
    assert updated.replay.attempts == 3
    assert updated.replay.reproduced == 3
