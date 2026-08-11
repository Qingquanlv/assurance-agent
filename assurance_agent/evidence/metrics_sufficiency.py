"""Adjudicate ``inspect/metrics.json`` against ``policy.evidence_sufficiency``.

This is the auditable Python consumer of ``floors``, ``cadence``,
``mutation_budget_seconds``, and — for numeric warn-band floor misses —
``on_insufficient``. Task 8's ``metrics-sufficiency-gate`` will route on the
same table (DSL or a builtin calling this function). Until then, the §12.8
truth-table guard in ``test_plan_check_gate`` proves changing those policy
values moves the verdict — the anti-pattern that guard forbids is a
tautological DSL reference that never changes outcome (the retired
``coverage_floor > 0`` form).

Routing order (Task-8 gate wiring must preserve this):

1. ``collection_gaps`` non-empty, or any floor-scoped metric is
   ``collection_failed`` / evaluated-but-unmeasurable → ``reject`` (fail-closed;
   never SKIPPED). This is "we don't have a number to judge", not "we have one
   and it fails" — mirrors ``trace-sufficiency-gate``'s own ``reject``: block
   release, but keep flowing to report / archive / retro instead of a graph
   STOP, so a broken collector cannot masquerade as an unreviewable dead end
   nor get silently rubber-stamped through ``needs_human`` → ``accept_risk``.
2. evaluated boolean floor miss (``holds`` ≠ ``must_hold``), cadence-independent;
   ``on_insufficient`` does not apply:
   - ``risk_tier == critical`` → ``stop``
   - lower tiers → ``needs_human``
   Open-counterexample hard rule (``adversarial_clean``) also applies when the
   metric is evaluated with ``holds=False`` even if the tier band omits the
   floor (packaged policy enables it on critical only).
3. empty cadence schedule for this document → ``skipped``
4. numeric floor miss for a cadence-scoped metric → by ``on_insufficient``:
   - ``require_human`` → ``needs_human`` (with ``below_floor`` shortboards)
   - ``warn`` → ``pass`` (with ``below_floor`` shortboards; do not stop)
   - ``block`` → ``stop`` (with ``below_floor`` shortboards)
5. otherwise ``pass``

``not_evaluated`` / absent measurement is not a floor fail and is distinct from
``collection_failed`` / typed gaps. Trace-sufficiency still DSL-reads
``on_insufficient`` for case-evidence gaps; this module reuses the same action
for numeric metric floors per design §6.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

from assurance_agent.artifacts.models.common import RiskTier
from assurance_agent.artifacts.models.metrics import (
    METRIC_KINDS,
    MetricEntry,
    MetricKey,
    MetricShortboard,
    MetricsDocument,
)
from assurance_agent.artifacts.models.policy import EvidenceSufficiency, MetricFloor, PlanCheckAction

MetricsSufficiencyVerdict = Literal["pass", "needs_human", "reject", "stop", "skipped"]


@dataclass(frozen=True)
class MetricsSufficiencyDecision:
    """Gate-facing decision.

    ``mutation_budget_seconds`` is copied from policy so a budget change is
    observable even when it does not flip the verdict (§5-B1: exceeding the
    budget is a shortboard on the document, never a fail here).

    ``shortboards`` carries ``below_floor`` entries for numeric misses so Task 8
    can attach the shortboard list to pass / needs_human / stop without
    re-deriving floors.
    """

    verdict: MetricsSufficiencyVerdict
    mutation_budget_seconds: int
    shortboards: tuple[MetricShortboard, ...] = field(default_factory=tuple)


def _measured_value(entry: MetricEntry, floor: MetricFloor) -> float | bool | None:
    if floor.target == "holds":
        return entry.holds
    if floor.surface is not None:
        for surface in entry.surfaces:
            if surface.layer == floor.surface:
                if floor.target == "touched":
                    return None if surface.touched is None else surface.touched.value
                return surface.value
        return None
    if floor.target == "touched":
        if entry.touched is None:
            return None
        return entry.touched.value
    return entry.value


def _boolean_floor_verdict(
    entry: MetricEntry,
    floor: MetricFloor,
    *,
    risk_tier: RiskTier,
) -> MetricsSufficiencyVerdict | None:
    """Return the verdict for a boolean floor miss, or None when satisfied.

    Measured miss: critical tier stops; lower tiers keep it visible as
    needs_human (M3 Task 2). Evaluated-but-unmeasurable (``holds`` absent, or no
    matching surface) is the "no number to judge" family → reject, same as the
    numeric side.
    """
    measured = _measured_value(entry, floor)
    if measured is None:
        return "reject"
    if measured != floor.must_hold:
        return "stop" if risk_tier == "critical" else "needs_human"
    return None


def _numeric_below_floor(entry: MetricEntry, floor: MetricFloor) -> bool | None:
    """True if below min; False if clears; None if measurement is incomplete."""
    measured = _measured_value(entry, floor)
    if measured is None or floor.min is None:
        return None
    return float(measured) < floor.min


def _numeric_miss_verdict(
    action: PlanCheckAction,
) -> MetricsSufficiencyVerdict:
    if action == "warn":
        return "pass"
    if action == "block":
        return "stop"
    return "needs_human"


def compute_floor_ratio(
    document: MetricsDocument,
    floors: Mapping[MetricKey, MetricFloor],
) -> float | None:
    """``min(min(actual/floor, 1.0))`` over enabled + evaluated numeric metrics (§7).

    A metric is enabled when the tier band declares a numeric floor (``min``).
    ``not_evaluated`` / null measurements and boolean ``holds`` floors are
    excluded. Returns ``None`` when nothing qualifies — never a vacuous 1.0.
    Named ``floor_ratio`` on purpose: never publish this as ``confidence``.
    """
    scores: list[float] = []
    for key, floor in floors.items():
        if floor.target == "holds" or floor.min is None or floor.min <= 0:
            continue
        if METRIC_KINDS.get(key) != "numeric":
            continue
        entry = document.metrics.get(key)
        if entry is None or entry.status != "evaluated":
            continue
        measured = _measured_value(entry, floor)
        if measured is None:
            continue
        scores.append(min(float(measured) / floor.min, 1.0))
    if not scores:
        return None
    return min(scores)


def numeric_below_floor_shortboards(
    document: MetricsDocument,
    floors: Mapping[MetricKey, MetricFloor],
) -> tuple[MetricShortboard, ...]:
    """Report-only ``below_floor`` boards for evaluated numeric floor misses.

    Boolean floors are checked separately via ``evaluate_metrics_sufficiency``
    (deterministic stop / needs_human) and do not enter this list.
    """
    shortboards: list[MetricShortboard] = []
    for key, floor in floors.items():
        if floor.target == "holds":
            continue
        entry = document.metrics.get(key)
        if entry is None or entry.status != "evaluated":
            continue
        below = _numeric_below_floor(entry, floor)
        if below is True:
            shortboards.append(
                MetricShortboard(
                    code="below_floor",
                    metric=key,
                    detail=f"{_measured_value(entry, floor)} < {floor.min}",
                )
            )
    return tuple(shortboards)


def evaluate_metrics_sufficiency(
    document: MetricsDocument,
    sufficiency: EvidenceSufficiency,
) -> MetricsSufficiencyDecision:
    """Compare one metrics document to the organisation's floors and cadence.

    See module docstring for the Task-8 routing table and ``on_insufficient`` map.
    """
    budget = sufficiency.mutation_budget_seconds
    schedule = set(sufficiency.cadence.pr if document.cadence == "pr" else sufficiency.cadence.nightly)
    band = sufficiency.floors[document.risk_tier]
    action = sufficiency.on_insufficient

    if document.collection_gaps:
        return MetricsSufficiencyDecision(verdict="reject", mutation_budget_seconds=budget)

    # Boolean floors first: deterministic checks apply when evaluated, even if the
    # cadence schedule is empty or never listed them (§8 / §6).
    for key, floor in band.items():
        if floor.target != "holds":
            continue
        entry = document.metrics.get(key)
        if entry is None or entry.status in ("skipped", "not_evaluated"):
            continue
        if entry.status == "collection_failed":
            return MetricsSufficiencyDecision(verdict="reject", mutation_budget_seconds=budget)
        failed = _boolean_floor_verdict(entry, floor, risk_tier=document.risk_tier)
        if failed is not None:
            return MetricsSufficiencyDecision(verdict=failed, mutation_budget_seconds=budget)

    # Open-counterexample hard rule when evaluated but omitted from this tier's floors
    # (packaged policy enables adversarial_clean on critical only).
    if "adversarial_clean" not in band:
        clean = document.metrics.get("adversarial_clean")
        if clean is not None and clean.status == "evaluated" and clean.holds is False:
            return MetricsSufficiencyDecision(verdict="needs_human", mutation_budget_seconds=budget)

    if not schedule:
        return MetricsSufficiencyDecision(verdict="skipped", mutation_budget_seconds=budget)

    shortboards: list[MetricShortboard] = []
    for key, floor in band.items():
        if floor.target == "holds":
            continue
        entry = document.metrics.get(key)
        if entry is None or entry.status in ("skipped", "not_evaluated"):
            continue
        if key not in schedule:
            continue
        if entry.status == "collection_failed":
            return MetricsSufficiencyDecision(verdict="reject", mutation_budget_seconds=budget)
        below = _numeric_below_floor(entry, floor)
        if below is None:
            # Evaluated-but-unmeasurable: fail-closed as a gap, not a warn-band shortboard.
            return MetricsSufficiencyDecision(verdict="reject", mutation_budget_seconds=budget)
        if below:
            metric_key: MetricKey = key
            shortboards.append(
                MetricShortboard(
                    code="below_floor",
                    metric=metric_key,
                    detail=f"{_measured_value(entry, floor)} < {floor.min}",
                )
            )

    if shortboards:
        return MetricsSufficiencyDecision(
            verdict=_numeric_miss_verdict(action),
            mutation_budget_seconds=budget,
            shortboards=tuple(shortboards),
        )

    return MetricsSufficiencyDecision(verdict="pass", mutation_budget_seconds=budget)


__all__ = [
    "MetricsSufficiencyDecision",
    "MetricsSufficiencyVerdict",
    "compute_floor_ratio",
    "evaluate_metrics_sufficiency",
    "numeric_below_floor_shortboards",
]
