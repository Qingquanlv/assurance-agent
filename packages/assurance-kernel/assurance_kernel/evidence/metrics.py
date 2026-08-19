"""Pure aggregation of PR/nightly evidence into ``MetricsDocument`` (§9 / §10).

This module is clock-free and I/O-free: callers inject ``computed_at`` and
``policy_digest``, and hand in already-loaded evidence artifacts plus a risk
resolution from ``evidence.risk_tier``. Same inputs always replay to the same
normalized subtree (``MetricsDocument.replay_subtree``); ``computed_at`` is
deliberately outside that anchor.

Side-effecting writers live in ``workflow.metrics.pr_metrics`` /
``workflow.metrics.nightly`` — they load evidence, call these functions, and
publish the registered carriers.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Protocol, runtime_checkable

from assurance_kernel.artifacts.models.metrics import (
    METRIC_LAYERS,
    WHOLE_METRIC_GAP_CODES,
    MetricCollectionGap,
    MetricEntry,
    MetricKey,
    MetricLayer,
    MetricScope,
    MetricShortboard,
    MetricSurface,
    MetricsDocument,
    RiskTierFacts,
)
from assurance_kernel.artifacts.models.policy import EvidenceSufficiency, MetricFloor
from assurance_kernel.artifacts.models.pr_metric_evidence import (
    AdversarialYieldEvidence,
    AssertionStrengthEvidence,
    AuthMatrixEvidence,
    BaselineDriftEvidence,
    ConstraintCoverageEvidence,
    CoverageDiffEvidence,
    JourneyCoverageEvidence,
    MutationEvidence,
    PerfSlackEvidence,
)
from assurance_kernel.evidence.metrics_sufficiency import (
    compute_floor_ratio,
    evaluate_metrics_sufficiency,
    numeric_below_floor_shortboards,
)

# Filenames published under ``execution/runs/<batch>/`` (PR collectors).
DIFF_EVIDENCE = "coverage-diff.json"
CONSTRAINT_EVIDENCE = "constraint-coverage.json"
AUTH_EVIDENCE = "auth-matrix.json"
JOURNEY_EVIDENCE = "journey-coverage.json"
SLACK_EVIDENCE = "perf-slack.json"

# Filenames published under ``execution/runs/nightly/`` (M2/M3 collectors).
MUTATION_EVIDENCE = "mutation.json"
ASSERTION_STRENGTH_EVIDENCE = "assertion-strength.json"
BASELINE_DRIFT_EVIDENCE = "baseline-drift.json"
ADVERSARIAL_YIELD_EVIDENCE = "adversarial-yield.json"

DEFAULT_PR_KEYS: tuple[MetricKey, ...] = (
    "diff_coverage",
    "constraint_coverage",
    "auth_matrix_coverage",
    "journey_coverage",
    "threshold_slack",
)
DEFAULT_NIGHTLY_KEYS: tuple[MetricKey, ...] = (
    "mutation_score",
    "assertion_strength",
    "adversarial_yield",
    "baseline_drift",
)


@runtime_checkable
class _ScopedEvidence(Protocol):
    change_id: str
    batch_id: str
    declared: MetricScope | None
    touched: MetricScope | None
    value: float | None
    collection_gaps: tuple[MetricCollectionGap, ...]
    shortboards: tuple[MetricShortboard, ...]


def _layer_for(key: MetricKey) -> MetricLayer:
    """Stable single layer for an entry (first of the key's allowed set)."""
    return sorted(METRIC_LAYERS[key])[0]


def _missing_pr_entry(key: MetricKey, detail: str) -> tuple[MetricEntry, MetricCollectionGap]:
    gap = MetricCollectionGap(code="collection_failed", metric=key, detail=detail)
    entry = MetricEntry(layer=_layer_for(key), status="collection_failed")
    return entry, gap


def _identity_gaps(
    *,
    key: MetricKey,
    change_id: str,
    evidence_change_id: str,
    evidence_batch_id: str,
    expected_batch_id: str | None,
) -> tuple[MetricCollectionGap, ...]:
    gaps: list[MetricCollectionGap] = []
    if evidence_change_id != change_id:
        gaps.append(
            MetricCollectionGap(
                code="identity_mismatch",
                metric=key,
                detail=f"evidence change_id={evidence_change_id!r} != {change_id!r}",
            )
        )
    if expected_batch_id is not None and evidence_batch_id != expected_batch_id:
        gaps.append(
            MetricCollectionGap(
                code="identity_mismatch",
                metric=key,
                detail=f"evidence batch_id={evidence_batch_id!r} != {expected_batch_id!r}",
            )
        )
    return tuple(gaps)


def _from_scalar(
    *,
    key: MetricKey,
    evidence_name: str,
    value: float | None,
    change_id: str,
    evidence_change_id: str,
    evidence_batch_id: str,
    expected_batch_id: str | None,
    collection_gaps: tuple[MetricCollectionGap, ...],
    shortboards: tuple[MetricShortboard, ...],
) -> tuple[MetricEntry, tuple[MetricCollectionGap, ...], tuple[MetricShortboard, ...]]:
    gaps = list(collection_gaps) + list(
        _identity_gaps(
            key=key,
            change_id=change_id,
            evidence_change_id=evidence_change_id,
            evidence_batch_id=evidence_batch_id,
            expected_batch_id=expected_batch_id,
        )
    )
    # Whole-metric gaps (empty subject) — including identity_mismatch — sink the entry.
    if any(g.is_whole_metric for g in gaps) or any(g.code in WHOLE_METRIC_GAP_CODES for g in gaps):
        return (
            MetricEntry(layer=_layer_for(key), status="collection_failed"),
            tuple(gaps),
            shortboards,
        )
    if value is None:
        # Collected but unmeasurable (e.g. no changed lines) — not a typed gap.
        return (
            MetricEntry(layer=_layer_for(key), status="not_evaluated"),
            tuple(gaps),
            shortboards,
        )
    return (
        MetricEntry(
            layer=_layer_for(key),
            status="evaluated",
            value=value,
            evidence=evidence_name,
        ),
        tuple(gaps),
        shortboards,
    )


def _from_scoped(
    *,
    key: MetricKey,
    evidence_name: str,
    evidence: _ScopedEvidence,
    change_id: str,
    expected_batch_id: str | None,
) -> tuple[MetricEntry, tuple[MetricCollectionGap, ...], tuple[MetricShortboard, ...]]:
    gaps = list(evidence.collection_gaps) + list(
        _identity_gaps(
            key=key,
            change_id=change_id,
            evidence_change_id=evidence.change_id,
            evidence_batch_id=evidence.batch_id,
            expected_batch_id=expected_batch_id,
        )
    )
    if any(g.is_whole_metric for g in gaps):
        return (
            MetricEntry(layer=_layer_for(key), status="collection_failed"),
            tuple(gaps),
            evidence.shortboards,
        )
    declared = evidence.declared
    if declared is None or evidence.value is None:
        # Empty denominator / failed collection without a whole-metric gap already
        # attached — still fail closed rather than invent 0/0.
        if not any(g.metric == key and g.is_whole_metric for g in gaps):
            gaps.append(
                MetricCollectionGap(
                    code="collection_failed",
                    metric=key,
                    detail=f"{evidence_name} has no measurable declared scope",
                )
            )
        return (
            MetricEntry(layer=_layer_for(key), status="collection_failed"),
            tuple(gaps),
            evidence.shortboards,
        )
    return (
        MetricEntry(
            layer=_layer_for(key),
            status="evaluated",
            value=evidence.value,
            declared=declared,
            touched=evidence.touched,
            evidence=evidence_name,
        ),
        tuple(gaps),
        evidence.shortboards,
    )


def _pending_nightly(key: MetricKey) -> tuple[MetricEntry, MetricShortboard]:
    return (
        MetricEntry(layer=_layer_for(key), status="not_evaluated"),
        MetricShortboard(code="pending_nightly", metric=key),
    )


def aggregate_pr_metrics(
    *,
    change_id: str,
    computed_at: datetime,
    policy_digest: str,
    risk: RiskTierFacts,
    coverage_diff: CoverageDiffEvidence | None,
    constraint_coverage: ConstraintCoverageEvidence | None,
    auth_matrix: AuthMatrixEvidence | None,
    journey_coverage: JourneyCoverageEvidence | None,
    perf_slack: PerfSlackEvidence | None,
    pr_keys: Sequence[MetricKey] = DEFAULT_PR_KEYS,
    nightly_keys: Sequence[MetricKey] = DEFAULT_NIGHTLY_KEYS,
    expected_batch_id: str | None = None,
    floor_ratio: float | None = None,
) -> MetricsDocument:
    """Fold the five PR evidence artifacts (+ nightly placeholders) into one document.

    Missing PR evidence is a whole-metric ``collection_failed`` gap. Nightly keys
    are ``not_evaluated`` with ``pending_nightly`` shortboards — never gaps.
    ``computed_at`` is recorded on the document but excluded from
    ``replay_subtree``.
    """
    metrics: dict[MetricKey, MetricEntry] = {}
    gaps: list[MetricCollectionGap] = []
    shortboards: list[MetricShortboard] = []

    pr_set = set(pr_keys)

    if "diff_coverage" in pr_set:
        if coverage_diff is None:
            entry, gap = _missing_pr_entry("diff_coverage", f"{DIFF_EVIDENCE} missing")
            metrics["diff_coverage"] = entry
            gaps.append(gap)
        else:
            entry, egaps, eboards = _from_scalar(
                key="diff_coverage",
                evidence_name=DIFF_EVIDENCE,
                value=coverage_diff.value,
                change_id=change_id,
                evidence_change_id=coverage_diff.change_id,
                evidence_batch_id=coverage_diff.batch_id,
                expected_batch_id=expected_batch_id,
                collection_gaps=coverage_diff.collection_gaps,
                shortboards=coverage_diff.shortboards,
            )
            metrics["diff_coverage"] = entry
            gaps.extend(egaps)
            shortboards.extend(eboards)

    if "constraint_coverage" in pr_set:
        if constraint_coverage is None:
            entry, gap = _missing_pr_entry("constraint_coverage", f"{CONSTRAINT_EVIDENCE} missing")
            metrics["constraint_coverage"] = entry
            gaps.append(gap)
        else:
            entry, egaps, eboards = _from_scoped(
                key="constraint_coverage",
                evidence_name=CONSTRAINT_EVIDENCE,
                evidence=constraint_coverage,
                change_id=change_id,
                expected_batch_id=expected_batch_id,
            )
            metrics["constraint_coverage"] = entry
            gaps.extend(egaps)
            shortboards.extend(eboards)

    if "auth_matrix_coverage" in pr_set:
        if auth_matrix is None:
            entry, gap = _missing_pr_entry("auth_matrix_coverage", f"{AUTH_EVIDENCE} missing")
            metrics["auth_matrix_coverage"] = entry
            gaps.append(gap)
        else:
            entry, egaps, eboards = _from_scoped(
                key="auth_matrix_coverage",
                evidence_name=AUTH_EVIDENCE,
                evidence=auth_matrix,
                change_id=change_id,
                expected_batch_id=expected_batch_id,
            )
            metrics["auth_matrix_coverage"] = entry
            gaps.extend(egaps)
            shortboards.extend(eboards)

    if "journey_coverage" in pr_set:
        if journey_coverage is None:
            entry, gap = _missing_pr_entry("journey_coverage", f"{JOURNEY_EVIDENCE} missing")
            metrics["journey_coverage"] = entry
            gaps.append(gap)
        else:
            entry, egaps, eboards = _from_scoped(
                key="journey_coverage",
                evidence_name=JOURNEY_EVIDENCE,
                evidence=journey_coverage,
                change_id=change_id,
                expected_batch_id=expected_batch_id,
            )
            metrics["journey_coverage"] = entry
            gaps.extend(egaps)
            shortboards.extend(eboards)

    if "threshold_slack" in pr_set:
        if perf_slack is None:
            entry, gap = _missing_pr_entry("threshold_slack", f"{SLACK_EVIDENCE} missing")
            metrics["threshold_slack"] = entry
            gaps.append(gap)
        else:
            entry, egaps, eboards = _from_scalar(
                key="threshold_slack",
                evidence_name=SLACK_EVIDENCE,
                value=perf_slack.value,
                change_id=change_id,
                evidence_change_id=perf_slack.change_id,
                evidence_batch_id=perf_slack.batch_id,
                expected_batch_id=expected_batch_id,
                collection_gaps=perf_slack.collection_gaps,
                shortboards=perf_slack.shortboards,
            )
            metrics["threshold_slack"] = entry
            gaps.extend(egaps)
            shortboards.extend(eboards)

    for key in nightly_keys:
        entry, board = _pending_nightly(key)
        metrics[key] = entry
        shortboards.append(board)

    # adversarial_clean is a boolean floor key not listed in either cadence; it is
    # derived on the nightly carrier from adversarial_yield (M3 Task 2), not PR.

    return MetricsDocument.of(
        risk=risk,
        change_id=change_id,
        cadence="pr",
        computed_at=computed_at,
        metrics=metrics,
        policy_digest=policy_digest,
        collection_gaps=tuple(gaps),
        shortboards=tuple(shortboards),
        floor_ratio=floor_ratio,
    )


def _ensure_pending_nightly(
    key: MetricKey,
    shortboards: Sequence[MetricShortboard],
) -> tuple[MetricShortboard, ...]:
    if any(board.code == "pending_nightly" and board.metric == key for board in shortboards):
        return tuple(shortboards)
    return tuple(shortboards) + (MetricShortboard(code="pending_nightly", metric=key),)


def _fold_mutation(
    *,
    change_id: str,
    evidence: MutationEvidence | None,
) -> tuple[MetricEntry, tuple[MetricCollectionGap, ...], tuple[MetricShortboard, ...]]:
    if evidence is None:
        entry, board = _pending_nightly("mutation_score")
        return entry, (), (board,)
    gaps = list(evidence.collection_gaps) + list(
        _identity_gaps(
            key="mutation_score",
            change_id=change_id,
            evidence_change_id=evidence.change_id,
            evidence_batch_id=evidence.batch_id,
            expected_batch_id=None,
        )
    )
    shortboards = tuple(evidence.shortboards)
    if any(g.is_whole_metric for g in gaps) or evidence.status == "collection_failed":
        if not any(g.is_whole_metric for g in gaps):
            gaps.append(
                MetricCollectionGap(
                    code="collection_failed",
                    metric="mutation_score",
                    detail="mutation evidence status=collection_failed",
                )
            )
        return (
            MetricEntry(layer=_layer_for("mutation_score"), status="collection_failed"),
            tuple(gaps),
            shortboards,
        )
    if evidence.status != "evaluated" or evidence.value is None:
        return (
            MetricEntry(layer=_layer_for("mutation_score"), status="not_evaluated"),
            tuple(gaps),
            _ensure_pending_nightly("mutation_score", shortboards),
        )
    return (
        MetricEntry(
            layer=_layer_for("mutation_score"),
            status="evaluated",
            value=evidence.value,
            evidence=MUTATION_EVIDENCE,
        ),
        tuple(gaps),
        shortboards,
    )


def _fold_assertion_strength(
    *,
    change_id: str,
    evidence: AssertionStrengthEvidence | None,
) -> tuple[MetricEntry, tuple[MetricCollectionGap, ...], tuple[MetricShortboard, ...]]:
    if evidence is None:
        entry, board = _pending_nightly("assertion_strength")
        return entry, (), (board,)
    gaps = list(evidence.collection_gaps) + list(
        _identity_gaps(
            key="assertion_strength",
            change_id=change_id,
            evidence_change_id=evidence.change_id,
            evidence_batch_id=evidence.batch_id,
            expected_batch_id=None,
        )
    )
    shortboards = tuple(evidence.shortboards)
    if any(g.is_whole_metric for g in gaps) or evidence.status == "collection_failed":
        if not any(g.is_whole_metric for g in gaps):
            gaps.append(
                MetricCollectionGap(
                    code="collection_failed",
                    metric="assertion_strength",
                    detail="assertion-strength evidence status=collection_failed",
                )
            )
        return (
            MetricEntry(layer=_layer_for("assertion_strength"), status="collection_failed"),
            tuple(gaps),
            shortboards,
        )
    if (
        evidence.status != "evaluated"
        or evidence.value is None
        or evidence.declared is None
        or not evidence.surfaces
    ):
        return (
            MetricEntry(layer=_layer_for("assertion_strength"), status="not_evaluated"),
            tuple(gaps),
            _ensure_pending_nightly("assertion_strength", shortboards),
        )
    surfaces = tuple(
        MetricSurface(
            layer=surface.layer,
            value=surface.value if surface.value is not None else 0.0,
            declared=surface.declared,
            evidence=ASSERTION_STRENGTH_EVIDENCE,
        )
        for surface in evidence.surfaces
    )
    return (
        MetricEntry(
            layer=_layer_for("assertion_strength"),
            status="evaluated",
            value=evidence.value,
            declared=evidence.declared,
            surfaces=surfaces,
            evidence=ASSERTION_STRENGTH_EVIDENCE,
        ),
        tuple(gaps),
        shortboards,
    )


def _fold_baseline_drift(
    *,
    change_id: str,
    evidence: BaselineDriftEvidence | None,
) -> tuple[MetricEntry, tuple[MetricCollectionGap, ...], tuple[MetricShortboard, ...]]:
    if evidence is None:
        entry, board = _pending_nightly("baseline_drift")
        return entry, (), (board,)
    gaps = list(evidence.collection_gaps) + list(
        _identity_gaps(
            key="baseline_drift",
            change_id=change_id,
            evidence_change_id=evidence.change_id,
            evidence_batch_id=evidence.batch_id,
            expected_batch_id=None,
        )
    )
    shortboards = tuple(evidence.shortboards)
    if any(g.is_whole_metric for g in gaps) or evidence.status == "collection_failed":
        if not any(g.is_whole_metric for g in gaps):
            gaps.append(
                MetricCollectionGap(
                    code="collection_failed",
                    metric="baseline_drift",
                    detail="baseline-drift evidence status=collection_failed",
                )
            )
        return (
            MetricEntry(layer=_layer_for("baseline_drift"), status="collection_failed"),
            tuple(gaps),
            shortboards,
        )
    if evidence.status != "evaluated" or evidence.value is None:
        return (
            MetricEntry(layer=_layer_for("baseline_drift"), status="not_evaluated"),
            tuple(gaps),
            _ensure_pending_nightly("baseline_drift", shortboards),
        )
    return (
        MetricEntry(
            layer=_layer_for("baseline_drift"),
            status="evaluated",
            value=evidence.value,
            evidence=BASELINE_DRIFT_EVIDENCE,
        ),
        tuple(gaps),
        shortboards,
    )


def _fold_adversarial_yield(
    *,
    change_id: str,
    evidence: AdversarialYieldEvidence | None,
) -> tuple[MetricEntry, tuple[MetricCollectionGap, ...], tuple[MetricShortboard, ...]]:
    if evidence is None:
        entry, board = _pending_nightly("adversarial_yield")
        return entry, (), (board,)
    gaps = list(evidence.collection_gaps) + list(
        _identity_gaps(
            key="adversarial_yield",
            change_id=change_id,
            evidence_change_id=evidence.change_id,
            evidence_batch_id=evidence.batch_id,
            expected_batch_id=None,
        )
    )
    shortboards = tuple(evidence.shortboards)
    if any(g.is_whole_metric for g in gaps) or evidence.status == "collection_failed":
        if not any(g.is_whole_metric for g in gaps):
            gaps.append(
                MetricCollectionGap(
                    code="collection_failed",
                    metric="adversarial_yield",
                    detail="adversarial-yield evidence status=collection_failed",
                )
            )
        return (
            MetricEntry(layer=_layer_for("adversarial_yield"), status="collection_failed"),
            tuple(gaps),
            shortboards,
        )
    if evidence.status != "evaluated" or evidence.value is None:
        return (
            MetricEntry(layer=_layer_for("adversarial_yield"), status="not_evaluated"),
            tuple(gaps),
            _ensure_pending_nightly("adversarial_yield", shortboards),
        )
    # Prefer evidence.layer when it is one of the key's allowed layers.
    layer = (
        evidence.layer
        if evidence.layer in METRIC_LAYERS["adversarial_yield"]
        else _layer_for("adversarial_yield")
    )
    return (
        MetricEntry(
            layer=layer,
            status="evaluated",
            value=evidence.value,
            evidence=ADVERSARIAL_YIELD_EVIDENCE,
        ),
        tuple(gaps),
        shortboards,
    )


def _fold_adversarial_clean(
    evidence: AdversarialYieldEvidence | None,
) -> tuple[MetricEntry, tuple[MetricShortboard, ...]]:
    """Derive ``adversarial_clean`` from evaluated yield; never invent holds=True.

    Callers pass ``None`` when the folded ``adversarial_yield`` entry is not
    ``evaluated`` (pending / collection_failed / identity gap). Explicit
    ``not_evaluated`` keeps the boolean floor miss path from firing.
    """
    if evidence is None or evidence.status != "evaluated" or evidence.value is None:
        return MetricEntry(layer=_layer_for("adversarial_clean"), status="not_evaluated"), ()
    holds = evidence.unclosed_count == 0
    boards: tuple[MetricShortboard, ...] = ()
    if not holds:
        boards = (
            MetricShortboard(
                code="adversarial_open",
                metric="adversarial_clean",
                detail=f"unclosed_count={evidence.unclosed_count}",
            ),
        )
    return (
        MetricEntry(
            layer=_layer_for("adversarial_clean"),
            status="evaluated",
            holds=holds,
            evidence=ADVERSARIAL_YIELD_EVIDENCE,
        ),
        boards,
    )


def aggregate_nightly_metrics(
    *,
    change_id: str,
    computed_at: datetime,
    policy_digest: str,
    risk: RiskTierFacts,
    mutation: MutationEvidence | None,
    assertion_strength: AssertionStrengthEvidence | None,
    baseline_drift: BaselineDriftEvidence | None,
    adversarial_yield: AdversarialYieldEvidence | None = None,
    floors: Mapping[MetricKey, MetricFloor] | None = None,
    nightly_keys: Sequence[MetricKey] = DEFAULT_NIGHTLY_KEYS,
    sufficiency: EvidenceSufficiency | None = None,
) -> MetricsDocument:
    """Fold nightly batch evidence into ``inspect/metrics-nightly.json`` shape.

    Missing / not-yet-collected keys stay ``not_evaluated`` + ``pending_nightly``
    (never invented collection gaps). ``adversarial_yield`` folds when batch
    evidence is present; absence stays pending. ``adversarial_clean`` is derived
    from evaluated yield (``holds = unclosed_count == 0``); pending / failed yield
    leaves clean as explicit ``not_evaluated`` (never invents ``holds=True``).
    ``floor_ratio`` is ``min(min(actual/floor, 1))`` over enabled and evaluated
    numeric metrics only; boolean floors are checked via
    ``evaluate_metrics_sufficiency`` when ``sufficiency`` is provided (or via the
    shared numeric shortboard helper for report-only below-floor boards).
    """
    metrics: dict[MetricKey, MetricEntry] = {}
    gaps: list[MetricCollectionGap] = []
    shortboards: list[MetricShortboard] = []
    key_set = set(nightly_keys)

    if "mutation_score" in key_set:
        entry, egaps, eboards = _fold_mutation(change_id=change_id, evidence=mutation)
        metrics["mutation_score"] = entry
        gaps.extend(egaps)
        shortboards.extend(eboards)

    if "assertion_strength" in key_set:
        entry, egaps, eboards = _fold_assertion_strength(change_id=change_id, evidence=assertion_strength)
        metrics["assertion_strength"] = entry
        gaps.extend(egaps)
        shortboards.extend(eboards)

    if "baseline_drift" in key_set:
        entry, egaps, eboards = _fold_baseline_drift(change_id=change_id, evidence=baseline_drift)
        metrics["baseline_drift"] = entry
        gaps.extend(egaps)
        shortboards.extend(eboards)

    clean_source: AdversarialYieldEvidence | None = adversarial_yield
    if "adversarial_yield" in key_set:
        entry, egaps, eboards = _fold_adversarial_yield(change_id=change_id, evidence=adversarial_yield)
        metrics["adversarial_yield"] = entry
        gaps.extend(egaps)
        shortboards.extend(eboards)
        # Clean tracks the folded yield status — never evaluate from a failed fold.
        clean_source = adversarial_yield if entry.status == "evaluated" else None

    # Boolean hard-rule input: always publish (evaluated or explicit not_evaluated).
    clean_entry, clean_boards = _fold_adversarial_clean(clean_source)
    metrics["adversarial_clean"] = clean_entry
    shortboards.extend(clean_boards)

    for key in nightly_keys:
        if key not in metrics:
            entry, board = _pending_nightly(key)
            metrics[key] = entry
            shortboards.append(board)

    band: Mapping[MetricKey, MetricFloor] = floors if floors is not None else {}
    # Draft without floor_ratio so compute_floor_ratio can read entries first.
    draft = MetricsDocument.of(
        risk=risk,
        change_id=change_id,
        cadence="nightly",
        computed_at=computed_at,
        metrics=metrics,
        policy_digest=policy_digest,
        collection_gaps=tuple(gaps),
        shortboards=tuple(shortboards),
        floor_ratio=None,
    )
    ratio = compute_floor_ratio(draft, band)
    below = numeric_below_floor_shortboards(draft, band)
    merged_boards = list(draft.shortboards)
    for board in below:
        if board not in merged_boards:
            merged_boards.append(board)

    document = MetricsDocument.of(
        risk=risk,
        change_id=change_id,
        cadence="nightly",
        computed_at=computed_at,
        metrics=metrics,
        policy_digest=policy_digest,
        collection_gaps=tuple(gaps),
        shortboards=tuple(merged_boards),
        floor_ratio=ratio,
    )

    # Boolean floors (and cadence-scoped numeric routing) stay in the shared
    # evaluator — independent of floor_ratio membership (§7).
    if sufficiency is not None:
        decision = evaluate_metrics_sufficiency(document, sufficiency)
        for board in decision.shortboards:
            if board not in document.shortboards:
                merged_boards.append(board)
        if len(merged_boards) != len(document.shortboards):
            document = MetricsDocument.of(
                risk=risk,
                change_id=change_id,
                cadence="nightly",
                computed_at=computed_at,
                metrics=metrics,
                policy_digest=policy_digest,
                collection_gaps=tuple(gaps),
                shortboards=tuple(merged_boards),
                floor_ratio=ratio,
            )

    return document


__all__ = [
    "ADVERSARIAL_YIELD_EVIDENCE",
    "ASSERTION_STRENGTH_EVIDENCE",
    "AUTH_EVIDENCE",
    "BASELINE_DRIFT_EVIDENCE",
    "CONSTRAINT_EVIDENCE",
    "DEFAULT_NIGHTLY_KEYS",
    "DEFAULT_PR_KEYS",
    "DIFF_EVIDENCE",
    "JOURNEY_EVIDENCE",
    "MUTATION_EVIDENCE",
    "SLACK_EVIDENCE",
    "aggregate_nightly_metrics",
    "aggregate_pr_metrics",
]
