"""Promotion feedstock helpers for future C2 (M4 deferred).

Pure counters over applied ``PromotionReceipt`` + ``RegressionCandidate`` pairs.
This module is **not** a MetricKey / MetricsDocument / nightly fold — M4 will
consume ``count_promoted_counterexamples`` later when C-layer metrics land.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from assurance_kernel.artifacts.models.promotion import PromotionReceipt, RegressionCandidate

__all__ = ["count_promoted_counterexamples"]


def count_promoted_counterexamples(
    receipts: Sequence[PromotionReceipt],
    candidates: Sequence[RegressionCandidate] | Mapping[str, RegressionCandidate],
) -> int:
    """Count unique counterexamples promoted via ``status=applied`` receipts.

    Feedstock signal for future C2 ("promoted counterexample" yield). Rules:

    - Only ``status == "applied"`` receipts count (rolled_back / other → 0).
    - Receipt ``candidate_id`` must resolve in ``candidates``; unknown → skip.
    - Counts distinct ``counterexample_id`` values (not raw receipt rows).

    No MetricKey / MetricsDocument / nightly aggregation here — M4 owns that.
    """
    by_id: Mapping[str, RegressionCandidate]
    if isinstance(candidates, Mapping):
        by_id = candidates
    else:
        by_id = {c.candidate_id: c for c in candidates}

    promoted: set[str] = set()
    for receipt in receipts:
        if receipt.status != "applied":
            continue
        candidate = by_id.get(receipt.candidate_id)
        if candidate is None:
            continue
        promoted.add(candidate.counterexample_id)
    return len(promoted)
