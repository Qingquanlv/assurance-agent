"""Deterministic counterexample replay for adversarial discovery (Phase 1).

Pure orchestration over an injected AttemptRunner. Same seed / oracle-set
digest / setup / actions on every attempt. Only full hard-oracle reproduction
is confirmable; environment failure and divergence become inconclusive evidence.

M3 Task 3 adds pure ``build_replay_attempt_receipts`` so workflow can persist
immutable C3 attempt receipts without putting I/O in this module.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from assurance_agent.artifacts.canonical import sha256_bytes
from assurance_agent.artifacts.models.discovery import (
    Counterexample,
    FindingStatus,
    OracleSpec,
    ReplayAttemptOutcome,
    ReplayAttemptReceipt,
)
from assurance_agent.verification.oracle import OracleObservation, OracleVerdict, evaluate_oracle

AttemptOutcome = Literal["ok", "environment_failure", "divergence"]
ReplayPath = Literal["confirmable", "not_reproduced", "inconclusive_evidence"]


@dataclass(frozen=True)
class ReplayAttemptSpec:
    """Fixed inputs for one independent replay attempt."""

    seed: int
    oracle_set_digest: str
    setup: Mapping[str, Any]
    actions: tuple[Mapping[str, Any], ...]
    attempt_index: int


@dataclass(frozen=True)
class AttemptResult:
    """Outcome of one runner attempt (observation or typed failure)."""

    observation: OracleObservation | None = None
    outcome: AttemptOutcome = "ok"
    detail: str = ""


@dataclass(frozen=True)
class ReplayAggregate:
    attempts: int
    reproduced: int
    attempt_verdicts: tuple[OracleVerdict, ...]
    path: ReplayPath
    oracle_set_digest: str
    seed: int
    attempt_results: tuple[AttemptResult, ...] = ()


@dataclass(frozen=True)
class AttemptClassification:
    """Shared confirm/replay aggregate over attempt results."""

    attempts: int
    reproduced: int
    attempt_verdicts: tuple[OracleVerdict, ...]
    path: ReplayPath
    finding_status: FindingStatus


class AttemptRunner(Protocol):
    """Injectable executor for one seeded replay attempt."""

    def run_attempt(self, spec: ReplayAttemptSpec) -> AttemptResult: ...


def replay_counterexample(
    ce: Counterexample,
    oracle: OracleSpec,
    runner: AttemptRunner,
    *,
    oracle_set_digest: str,
    attempts: int | None = None,
) -> ReplayAggregate:
    """Replay CE N times with fixed seed/digest/payload; aggregate verdicts."""
    n = ce.replay.attempts if attempts is None else attempts
    if n < 1:
        raise ValueError("replay attempts must be >= 1")

    results: list[AttemptResult] = []
    for index in range(n):
        spec = ReplayAttemptSpec(
            seed=ce.seed,
            oracle_set_digest=oracle_set_digest,
            setup=dict(ce.setup),
            actions=tuple(dict(action) for action in ce.actions),
            attempt_index=index,
        )
        results.append(runner.run_attempt(spec))

    classification = classify_attempt_results(oracle, results)
    return ReplayAggregate(
        attempts=classification.attempts,
        reproduced=classification.reproduced,
        attempt_verdicts=classification.attempt_verdicts,
        path=classification.path,
        oracle_set_digest=oracle_set_digest,
        seed=ce.seed,
        attempt_results=tuple(results),
    )


def build_replay_attempt_receipts(
    *,
    counterexample_id: str,
    seed: int,
    base_revision: str,
    oracle_set_digest: str,
    oracle: OracleSpec,
    attempt_results: Sequence[AttemptResult],
    recorded_at: str | None = None,
) -> tuple[ReplayAttemptReceipt, ...]:
    """Map attempt results to immutable C3 receipts (pure; no I/O)."""
    receipts: list[ReplayAttemptReceipt] = []
    for index, result in enumerate(attempt_results):
        outcome, observed_digest, observed_status = _classify_attempt_for_receipt(oracle, result)
        receipts.append(
            ReplayAttemptReceipt(
                schema_version="1",
                counterexample_id=counterexample_id,
                seed=seed,
                attempt_index=index,
                base_revision=base_revision,
                oracle_set_digest=oracle_set_digest,
                outcome=outcome,
                observed_digest=observed_digest,
                observed_status=observed_status,
                recorded_at=recorded_at,
            )
        )
    return tuple(receipts)


def _classify_attempt_for_receipt(
    oracle: OracleSpec,
    result: AttemptResult,
) -> tuple[ReplayAttemptOutcome, str, int | None]:
    if result.outcome == "environment_failure":
        return "environment_failure", _empty_observation_digest(), None
    if result.outcome == "divergence":
        return "divergence", _empty_observation_digest(), None
    if result.observation is None:
        return "inconclusive", _empty_observation_digest(), None

    obs = result.observation
    digest = _observation_digest(obs)
    status = obs.status_code
    verdict = evaluate_oracle(oracle, obs)
    if verdict.kind == "violate":
        return "violate", digest, status
    if verdict.kind == "hold":
        return "hold", digest, status
    return "inconclusive", digest, status


def _observation_digest(observation: OracleObservation) -> str:
    payload = {
        "claims": dict(observation.claims) if observation.claims else None,
        "env_facts": dict(observation.env_facts) if observation.env_facts else None,
        "status_code": observation.status_code,
    }
    return sha256_bytes(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())


def _empty_observation_digest() -> str:
    return sha256_bytes(b"{}")


def classify_attempt_results(
    oracle: OracleSpec,
    attempt_results: Sequence[AttemptResult],
) -> AttemptClassification:
    """Classify attempt results into replay path + finding_status (pure)."""
    n = len(attempt_results)
    if n < 1:
        raise ValueError("attempt_results must be non-empty")

    verdicts: list[OracleVerdict] = []
    reproduced = 0
    saw_env_or_divergence = False
    saw_inconclusive = False
    saw_heuristic = False

    for result in attempt_results:
        if result.outcome in {"environment_failure", "divergence"}:
            saw_env_or_divergence = True
            verdicts.append(OracleVerdict(kind="inconclusive", reason_code=result.outcome))
            continue
        if result.observation is None:
            saw_inconclusive = True
            verdicts.append(OracleVerdict(kind="inconclusive", reason_code="missing_observation"))
            continue

        verdict = evaluate_oracle(oracle, result.observation)
        verdicts.append(verdict)
        if verdict.kind == "violate":
            reproduced += 1
        elif verdict.kind == "inconclusive":
            saw_inconclusive = True
        elif verdict.kind == "heuristic_signal":
            saw_heuristic = True

    path, finding_status = _path_and_status(
        oracle_kind=oracle.kind,
        attempts=n,
        reproduced=reproduced,
        saw_env_or_divergence=saw_env_or_divergence,
        saw_inconclusive=saw_inconclusive,
        saw_heuristic=saw_heuristic,
    )
    return AttemptClassification(
        attempts=n,
        reproduced=reproduced,
        attempt_verdicts=tuple(verdicts),
        path=path,
        finding_status=finding_status,
    )


def _path_and_status(
    *,
    oracle_kind: str,
    attempts: int,
    reproduced: int,
    saw_env_or_divergence: bool,
    saw_inconclusive: bool,
    saw_heuristic: bool,
) -> tuple[ReplayPath, FindingStatus]:
    if saw_env_or_divergence or saw_inconclusive:
        return "inconclusive_evidence", "inconclusive_evidence"

    if oracle_kind == "search_heuristic":
        # Heuristic signals never confirm; full signal → needs_review.
        return "not_reproduced", "needs_review"

    if oracle_kind != "hard_oracle":
        # environment_oracle and other non-hard kinds cannot confirm product findings.
        if reproduced == attempts:
            return "not_reproduced", "inconclusive_evidence"
        return "not_reproduced", "needs_review"

    if reproduced == attempts:
        return "confirmable", "confirmed"
    if saw_heuristic:
        return "not_reproduced", "needs_review"
    return "not_reproduced", "needs_review"
