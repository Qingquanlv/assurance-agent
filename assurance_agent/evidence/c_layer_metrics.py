"""Pure C1/C2/C3 aggregate → ``CLayerMetricsDocument`` (report-only).

Composes landed feedstock helpers; never invents vacuous rates; never merges
C2 promotion with gap-closure into one ratio; never aliases ``confidence``.
Accepts pre-validated typed inputs only — missing ``None`` feedstock yields
``not_evaluated`` (not a collection gap). Corrupt disk I/O is the writer's
concern, not this pure fold.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime

from assurance_agent.artifacts.canonical import canonical_json_bytes, sha256_bytes
from assurance_agent.artifacts.models.c_layer import CLayerMetricEntry, CLayerMetricsDocument
from assurance_agent.artifacts.models.coverage_gaps import CoverageGapsDocument
from assurance_agent.artifacts.models.discovery import ReplayAttemptReceipt
from assurance_agent.artifacts.models.issues import Problem
from assurance_agent.artifacts.models.promotion import PromotionReceipt, RegressionCandidate
from assurance_agent.evidence.coverage_gaps import count_closed_gaps, gap_identity
from assurance_agent.evidence.escape import compute_escape_rate
from assurance_agent.evidence.promotion import count_promoted_counterexamples
from assurance_agent.evidence.replay_telemetry import compute_seed_replay_rate, receipt_payload_digest

__all__ = ["aggregate_c_layer_metrics"]


def _missing() -> CLayerMetricEntry:
    return CLayerMetricEntry(status="not_evaluated")


def _empty_denom(*, evidence_digests: tuple[str, ...] = ()) -> CLayerMetricEntry:
    return CLayerMetricEntry(
        status="not_evaluated",
        numerator=0,
        denominator=0,
        rate=None,
        evidence_digests=evidence_digests,
    )


def _evaluated(
    numerator: int,
    denominator: int,
    *,
    evidence_digests: tuple[str, ...] = (),
) -> CLayerMetricEntry:
    return CLayerMetricEntry(
        status="evaluated",
        numerator=numerator,
        denominator=denominator,
        rate=numerator / denominator,
        evidence_digests=evidence_digests,
    )


def _digest_payload(payload: object) -> str:
    return sha256_bytes(canonical_json_bytes(payload))


def _escape_entry(problems: Sequence[Problem] | None) -> CLayerMetricEntry:
    if problems is None:
        return _missing()
    escapes, analyzed, rate = compute_escape_rate(problems)
    digests = (
        _digest_payload(
            {
                "escapes": escapes,
                "analyzed": analyzed,
                "problem_ids": [p.problem_id for p in problems],
            }
        ),
    )
    if rate is None:
        return _empty_denom(evidence_digests=digests)
    return _evaluated(escapes, analyzed, evidence_digests=digests)


def _promotion_entry(
    receipts: Sequence[PromotionReceipt] | None,
    candidates: Sequence[RegressionCandidate] | Mapping[str, RegressionCandidate] | None,
    total_counterexamples: int | None,
) -> CLayerMetricEntry:
    if receipts is None or candidates is None or total_counterexamples is None:
        return _missing()
    if total_counterexamples < 0:
        raise ValueError("total_counterexamples must be >= 0")
    promoted = count_promoted_counterexamples(receipts, candidates)
    digests = (
        _digest_payload(
            {
                "promoted": promoted,
                "total_counterexamples": total_counterexamples,
                "receipt_ids": sorted(r.receipt_id for r in receipts),
            }
        ),
    )
    if total_counterexamples == 0:
        return _empty_denom(evidence_digests=digests)
    if promoted > total_counterexamples:
        raise ValueError(
            f"promoted counterexamples ({promoted}) exceed total_counterexamples ({total_counterexamples})"
        )
    return _evaluated(promoted, total_counterexamples, evidence_digests=digests)


def _gap_closure_entry(
    previous: CoverageGapsDocument | None,
    current: CoverageGapsDocument | None,
) -> CLayerMetricEntry:
    if previous is None or current is None:
        return _missing()
    denom = len({gap_identity(gap) for gap in previous.gaps})
    closed = count_closed_gaps(previous, current)
    digests = (
        previous.projection_digest,
        current.projection_digest,
        _digest_payload({"closed": closed, "previous_gap_identities": denom}),
    )
    if denom == 0:
        return _empty_denom(evidence_digests=digests)
    return _evaluated(closed, denom, evidence_digests=digests)


def _replay_entry(receipts: Sequence[ReplayAttemptReceipt] | None) -> CLayerMetricEntry:
    if receipts is None:
        return _missing()
    success, attempts, rate = compute_seed_replay_rate(receipts)
    digests = tuple(sorted(receipt_payload_digest(r) for r in receipts))
    if rate is None:
        return _empty_denom(evidence_digests=digests)
    return _evaluated(success, attempts, evidence_digests=digests)


def aggregate_c_layer_metrics(
    *,
    problems: Sequence[Problem] | None,
    promotion_receipts: Sequence[PromotionReceipt] | None,
    candidates: Sequence[RegressionCandidate] | Mapping[str, RegressionCandidate] | None,
    total_counterexamples: int | None,
    previous_gaps: CoverageGapsDocument | None,
    current_gaps: CoverageGapsDocument | None,
    replay_receipts: Sequence[ReplayAttemptReceipt] | None,
    change_id: str,
    computed_at: datetime,
) -> CLayerMetricsDocument:
    """Fold feedstock into four independent report-only C-layer vectors.

    ``None`` for a feedstock argument means that vector is missing (not a gap,
    not a fabricated zero). Empty present feedstock with zero denominator yields
    ``not_evaluated`` with ``0/0`` and ``rate=None``.
    """
    return CLayerMetricsDocument(
        schema_version="1",
        change_id=change_id,
        cadence="report",
        computed_at=computed_at,
        escape_rate=_escape_entry(problems),
        counterexample_promotion_rate=_promotion_entry(promotion_receipts, candidates, total_counterexamples),
        coverage_gap_closure_rate=_gap_closure_entry(previous_gaps, current_gaps),
        seed_replay_stability=_replay_entry(replay_receipts),
    )
