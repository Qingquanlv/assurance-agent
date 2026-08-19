"""Frozen oracle evaluation for change-local adversarial discovery (Phase 1).

Pure functions only — no I/O. Hard oracles may return ``violate``; search
heuristics never do (``heuristic_signal`` / ``needs_review`` only). Confirmed
product findings require hard oracle + full deterministic replay.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from assurance_kernel.artifacts.models.discovery import (
    AuthIsolationRule,
    Counterexample,
    CounterexampleReplay,
    OracleSetSnapshot,
    OracleSpec,
    StatusCodeRule,
)

OracleVerdictKind = Literal["hold", "violate", "inconclusive", "heuristic_signal"]

_SUCCESS_STATUSES = frozenset(range(200, 300))
_DENY_STATUSES = frozenset({401, 403, 404})


@dataclass(frozen=True)
class OracleObservation:
    """Structured observation payload for oracle evaluation."""

    status_code: int | None = None
    claims: Mapping[str, Any] | None = None
    env_facts: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class OracleVerdict:
    kind: OracleVerdictKind
    reason_code: str


def evaluate_oracle(oracle: OracleSpec, observation: OracleObservation) -> OracleVerdict:
    """Evaluate a frozen ``OracleSpec`` against a structured observation."""
    if oracle.kind == "environment_oracle" and not observation.env_facts:
        return OracleVerdict(kind="inconclusive", reason_code="missing_env_facts")

    raw = _evaluate_rule(oracle.rule.status_codes, oracle.rule.auth_isolation, observation)
    if oracle.kind == "search_heuristic" and raw.kind == "violate":
        return OracleVerdict(kind="heuristic_signal", reason_code=raw.reason_code)
    return raw


def _evaluate_rule(
    status_codes: StatusCodeRule | None,
    auth_isolation: AuthIsolationRule | None,
    observation: OracleObservation,
) -> OracleVerdict:
    verdicts: list[OracleVerdict] = []
    if status_codes is not None:
        verdicts.append(_evaluate_status_codes(status_codes, observation))
    if auth_isolation is not None:
        verdicts.append(_evaluate_auth_isolation(auth_isolation, observation))
    if not verdicts:
        return OracleVerdict(kind="inconclusive", reason_code="empty_rule")
    return _combine_verdicts(verdicts)


def _combine_verdicts(verdicts: Sequence[OracleVerdict]) -> OracleVerdict:
    for verdict in verdicts:
        if verdict.kind == "violate":
            return verdict
    for verdict in verdicts:
        if verdict.kind == "inconclusive":
            return verdict
    for verdict in verdicts:
        if verdict.kind == "heuristic_signal":
            return verdict
    return verdicts[0]


def _evaluate_status_codes(rule: StatusCodeRule, observation: OracleObservation) -> OracleVerdict:
    status = observation.status_code
    if status is None:
        return OracleVerdict(kind="inconclusive", reason_code="missing_status_code")

    if rule.denied_codes and status in rule.denied_codes:
        return OracleVerdict(kind="violate", reason_code="denied_status_code")
    if rule.allowed_codes:
        if status in rule.allowed_codes:
            return OracleVerdict(kind="hold", reason_code="allowed_status_code")
        return OracleVerdict(kind="violate", reason_code="status_not_allowed")
    # denied-only rule; status not in denied
    return OracleVerdict(kind="hold", reason_code="status_not_denied")


def _evaluate_auth_isolation(rule: AuthIsolationRule, observation: OracleObservation) -> OracleVerdict:
    if rule.mode == "require_auth":
        return _evaluate_require_auth(rule, observation)
    return _evaluate_cross_boundary(rule, observation)


def _evaluate_require_auth(rule: AuthIsolationRule, observation: OracleObservation) -> OracleVerdict:
    claim_name = rule.subject_claim
    if not claim_name:
        return OracleVerdict(kind="inconclusive", reason_code="missing_claim_config")
    claims = observation.claims or {}
    subject = claims.get(claim_name)
    if subject is None or subject == "":
        if observation.status_code is None:
            return OracleVerdict(kind="inconclusive", reason_code="missing_status_code")
        if observation.status_code in _SUCCESS_STATUSES:
            return OracleVerdict(kind="violate", reason_code="unauthenticated_allowed")
        if observation.status_code in _DENY_STATUSES:
            return OracleVerdict(kind="hold", reason_code="unauthenticated_denied")
        return OracleVerdict(kind="inconclusive", reason_code="ambiguous_status")
    return OracleVerdict(kind="hold", reason_code="authenticated")


def _evaluate_cross_boundary(rule: AuthIsolationRule, observation: OracleObservation) -> OracleVerdict:
    if not rule.subject_claim or not rule.resource_claim:
        return OracleVerdict(kind="inconclusive", reason_code="missing_claim_config")
    claims = observation.claims or {}
    subject = claims.get(rule.subject_claim)
    resource = claims.get(rule.resource_claim)
    if subject is None or resource is None or subject == "" or resource == "":
        return OracleVerdict(kind="inconclusive", reason_code="missing_claim_value")
    if subject == resource:
        reason = "same_tenant" if rule.mode == "deny_cross_tenant" else "same_role"
        return OracleVerdict(kind="hold", reason_code=reason)

    if observation.status_code is None:
        return OracleVerdict(kind="inconclusive", reason_code="missing_status_code")
    if observation.status_code in _SUCCESS_STATUSES:
        reason = "cross_tenant_allowed" if rule.mode == "deny_cross_tenant" else "cross_role_allowed"
        return OracleVerdict(kind="violate", reason_code=reason)
    if observation.status_code in _DENY_STATUSES:
        reason = "cross_tenant_denied" if rule.mode == "deny_cross_tenant" else "cross_role_denied"
        return OracleVerdict(kind="hold", reason_code=reason)
    return OracleVerdict(kind="inconclusive", reason_code="ambiguous_status")


def confirm_counterexample(
    ce: Counterexample,
    oracle_set: OracleSetSnapshot,
    attempt_results: Sequence[Any],
) -> Counterexample:
    """Update CE finding_status from attempt results (pure; returns new CE).

    Confirmed only when the oracle is ``hard_oracle`` and every attempt
    yields a product-confirmable ``violate``. Search heuristics become
    ``needs_review``; environment / missing evidence → ``inconclusive_evidence``.
    """
    # Local import keeps oracle↔replay cycle free at module load for callers
    # that only need evaluate_oracle.
    from assurance_kernel.verification.replay import classify_attempt_results

    results = tuple(attempt_results)
    oracle = _lookup_oracle(oracle_set, ce.oracle_id)
    classification = classify_attempt_results(oracle, results)

    finding_status = classification.finding_status
    replay = CounterexampleReplay(
        attempts=classification.attempts,
        reproduced=classification.reproduced,
        artifact_refs=ce.replay.artifact_refs,
    )
    return ce.model_copy(
        update={
            "oracle_kind": oracle.kind,
            "finding_status": finding_status,
            "replay": replay,
        }
    )


def _lookup_oracle(oracle_set: OracleSetSnapshot, oracle_id: str) -> OracleSpec:
    for oracle in oracle_set.oracles:
        if oracle.oracle_id == oracle_id:
            return oracle
    raise KeyError(f"oracle_id not in oracle set: {oracle_id}")


@dataclass(frozen=True)
class FrozenOracleEval:
    """One frozen-oracle evaluation against a structured observation."""

    oracle_id: str
    oracle: OracleSpec
    verdict: OracleVerdict


def evaluate_frozen_oracles(
    oracle_set: OracleSetSnapshot,
    pairs: Sequence[tuple[str, OracleObservation]],
) -> tuple[FrozenOracleEval, ...]:
    """Evaluate each ``(oracle_id, observation)`` against the frozen oracle set.

    Pure composition of ``evaluate_oracle``. Unknown oracle ids raise ``KeyError``.
    """
    out: list[FrozenOracleEval] = []
    for oracle_id, observation in pairs:
        oracle = _lookup_oracle(oracle_set, oracle_id)
        out.append(
            FrozenOracleEval(
                oracle_id=oracle_id,
                oracle=oracle,
                verdict=evaluate_oracle(oracle, observation),
            )
        )
    return tuple(out)


# Re-export AttemptResult typing helper for confirm callers (optional).
__all__ = [
    "FrozenOracleEval",
    "OracleObservation",
    "OracleVerdict",
    "OracleVerdictKind",
    "confirm_counterexample",
    "evaluate_frozen_oracles",
    "evaluate_oracle",
]
