"""Task 7: pure PR metrics aggregation (evidence/metrics.py).

Same inputs → identical replay_subtree bytes; ``computed_at`` is injected and
excluded from the anchor; collection gaps stay distinct from ``not_evaluated``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from assurance_agent.artifacts.canonical import canonical_json_bytes
from assurance_agent.artifacts.models.cases import CaseEntry
from assurance_agent.artifacts.models.metrics import (
    MetricCollectionGap,
    MetricEntry,
    MetricScope,
    MetricShortboard,
)
from assurance_agent.artifacts.models.pr_metric_evidence import (
    AuthMatrixEvidence,
    ConstraintCoverageEvidence,
    CoverageDiffEvidence,
    JourneyCoverageEvidence,
    PerfSlackEvidence,
)
from assurance_agent.evidence.metrics import aggregate_pr_metrics
from assurance_agent.evidence.risk_tier import resolve_risk_tier

CHANGE_ID = "CH-AGG-001"
DIGEST = "a" * 64
COMPUTED_AT = datetime(2026, 8, 5, 10, 0, tzinfo=UTC)
BATCH_ID = "20260805-100000"

DEFAULT_PR = (
    "diff_coverage",
    "constraint_coverage",
    "auth_matrix_coverage",
    "journey_coverage",
    "threshold_slack",
)
DEFAULT_NIGHTLY = (
    "mutation_score",
    "assertion_strength",
    "adversarial_yield",
    "baseline_drift",
)


def _case(**overrides: Any) -> CaseEntry:
    payload: dict[str, Any] = {
        "case_id": "TC_API_001",
        "title": "create api metadata",
        "status": "active",
        "priority": "P1",
        "severity": "major",
        "type": "API",
        "module": "system.api",
    }
    payload.update(overrides)
    return CaseEntry.model_validate(payload)


def _diff(**overrides: Any) -> CoverageDiffEvidence:
    payload: dict[str, Any] = {
        "schema_version": "1",
        "change_id": CHANGE_ID,
        "batch_id": BATCH_ID,
        "total_changed_lines": 4,
        "covered_changed_lines": 3,
        "value": 0.75,
        "files": (),
        "collection_gaps": (),
        "shortboards": (),
    }
    payload.update(overrides)
    return CoverageDiffEvidence.model_validate(payload)


def _constraint(**overrides: Any) -> ConstraintCoverageEvidence:
    declared = MetricScope.of(total=4, covered=4)
    payload: dict[str, Any] = {
        "schema_version": "1",
        "change_id": CHANGE_ID,
        "batch_id": BATCH_ID,
        "declared": declared,
        "touched": MetricScope.of(total=2, covered=2),
        "value": 1.0,
        "collection_gaps": (),
        "shortboards": (),
    }
    payload.update(overrides)
    return ConstraintCoverageEvidence.model_validate(payload)


def _auth(**overrides: Any) -> AuthMatrixEvidence:
    declared = MetricScope.of(total=2, covered=2)
    payload: dict[str, Any] = {
        "schema_version": "1",
        "change_id": CHANGE_ID,
        "batch_id": BATCH_ID,
        "declared": declared,
        "touched": MetricScope.of(total=1, covered=1),
        "value": 1.0,
        "cells": (),
        "collection_gaps": (),
        "shortboards": (),
    }
    payload.update(overrides)
    return AuthMatrixEvidence.model_validate(payload)


def _journey(**overrides: Any) -> JourneyCoverageEvidence:
    declared = MetricScope.of(total=3, covered=2, uncovered=("journey.c",))
    payload: dict[str, Any] = {
        "schema_version": "1",
        "change_id": CHANGE_ID,
        "batch_id": BATCH_ID,
        "declared": declared,
        "touched": MetricScope.of(total=1, covered=1),
        "value": declared.value,
        "items": (),
        "collection_gaps": (),
        "shortboards": (),
    }
    payload.update(overrides)
    return JourneyCoverageEvidence.model_validate(payload)


def _slack(**overrides: Any) -> PerfSlackEvidence:
    payload: dict[str, Any] = {
        "schema_version": "1",
        "change_id": CHANGE_ID,
        "batch_id": BATCH_ID,
        "value": 4.0,
        "slack_band": 10.0,
        "scenarios": (),
        "collection_gaps": (),
        "shortboards": (),
    }
    payload.update(overrides)
    return PerfSlackEvidence.model_validate(payload)


def _aggregate(**overrides: Any):
    risk = resolve_risk_tier([_case()])
    payload: dict[str, Any] = {
        "change_id": CHANGE_ID,
        "computed_at": COMPUTED_AT,
        "policy_digest": DIGEST,
        "risk": risk,
        "coverage_diff": _diff(),
        "constraint_coverage": _constraint(),
        "auth_matrix": _auth(),
        "journey_coverage": _journey(),
        "perf_slack": _slack(),
        "pr_keys": DEFAULT_PR,
        "nightly_keys": DEFAULT_NIGHTLY,
    }
    payload.update(overrides)
    return aggregate_pr_metrics(**payload)


def test_same_inputs_replay_to_identical_normalized_subtree_bytes() -> None:
    first = _aggregate()
    second = _aggregate()
    assert first.replay_subtree() == second.replay_subtree()
    assert canonical_json_bytes(first.replay_subtree()) == canonical_json_bytes(second.replay_subtree())


def test_computed_at_is_excluded_from_replay_anchor() -> None:
    early = _aggregate(computed_at=datetime(2026, 8, 5, 1, 0, tzinfo=UTC))
    late = _aggregate(computed_at=datetime(2026, 8, 5, 23, 0, tzinfo=UTC))
    assert early.computed_at != late.computed_at
    assert early.replay_subtree() == late.replay_subtree()
    assert "computed_at" not in early.replay_subtree()


def test_producer_order_of_gaps_does_not_change_replay_bytes() -> None:
    gaps = (
        MetricCollectionGap(
            code="entity_without_constraints",
            metric="constraint_coverage",
            subject="role",
        ),
        MetricCollectionGap(
            code="property_unknown_key",
            metric="constraint_coverage",
            subject="entities.x.constraints.y",
        ),
    )
    forward = _aggregate(constraint_coverage=_constraint(collection_gaps=gaps))
    reversed_ = _aggregate(constraint_coverage=_constraint(collection_gaps=tuple(reversed(gaps))))
    assert forward.replay_subtree() == reversed_.replay_subtree()


def test_missing_pr_evidence_is_collection_gap_not_not_evaluated() -> None:
    doc = _aggregate(coverage_diff=None)
    entry = doc.metrics["diff_coverage"]
    assert entry.status == "collection_failed"
    assert any(
        gap.metric == "diff_coverage" and gap.code == "collection_failed" and not gap.subject
        for gap in doc.collection_gaps
    )
    assert entry.status != "not_evaluated"


def test_nightly_metrics_are_not_evaluated_with_pending_shortboard() -> None:
    doc = _aggregate()
    for key in DEFAULT_NIGHTLY:
        assert doc.metrics[key].status == "not_evaluated"
        assert doc.metrics[key].value is None
        assert doc.metrics[key].declared is None
    pending = {board.metric for board in doc.shortboards if board.code == "pending_nightly"}
    assert pending == set(DEFAULT_NIGHTLY)
    # Nightly absence must not invent whole-metric collection gaps.
    assert not any(gap.metric in DEFAULT_NIGHTLY for gap in doc.collection_gaps)


def test_subject_scoped_gap_coexists_with_partial_evaluation() -> None:
    gap = MetricCollectionGap(
        code="entity_without_constraints",
        metric="constraint_coverage",
        subject="dept",
    )
    doc = _aggregate(constraint_coverage=_constraint(collection_gaps=(gap,)))
    entry = doc.metrics["constraint_coverage"]
    assert entry.status == "evaluated"
    assert entry.value == 1.0
    assert any(g.subject == "dept" for g in doc.collection_gaps)


def test_whole_metric_gap_on_evidence_marks_collection_failed() -> None:
    gap = MetricCollectionGap(
        code="artifact_corrupt",
        metric="threshold_slack",
        detail="perf result unreadable",
    )
    doc = _aggregate(
        perf_slack=_slack(
            value=None,
            collection_gaps=(gap,),
        )
    )
    assert doc.metrics["threshold_slack"].status == "collection_failed"
    assert doc.metrics["threshold_slack"].value is None
    assert any(g.code == "artifact_corrupt" for g in doc.collection_gaps)


def test_scoped_ratio_publishes_declared_and_touched() -> None:
    doc = _aggregate()
    constraint = doc.metrics["constraint_coverage"]
    assert isinstance(constraint, MetricEntry)
    assert constraint.declared == MetricScope.of(total=4, covered=4)
    assert constraint.touched == MetricScope.of(total=2, covered=2)
    assert constraint.evidence == "constraint-coverage.json"


def test_scalars_publish_value_without_scope() -> None:
    doc = _aggregate()
    assert doc.metrics["diff_coverage"].value == 0.75
    assert doc.metrics["diff_coverage"].declared is None
    assert doc.metrics["threshold_slack"].value == 4.0
    assert doc.metrics["threshold_slack"].declared is None


def test_scalar_unmeasurable_value_is_not_evaluated_without_invented_gap() -> None:
    """Collected but unmeasurable scalars (e.g. zero changed lines) stay not_evaluated."""
    doc = _aggregate(
        coverage_diff=_diff(total_changed_lines=0, covered_changed_lines=0, value=None),
        perf_slack=_slack(value=None),
    )
    assert doc.metrics["diff_coverage"].status == "not_evaluated"
    assert doc.metrics["diff_coverage"].value is None
    assert doc.metrics["threshold_slack"].status == "not_evaluated"
    assert doc.metrics["threshold_slack"].value is None
    assert not any(gap.metric == "diff_coverage" for gap in doc.collection_gaps)
    assert not any(gap.metric == "threshold_slack" for gap in doc.collection_gaps)


def test_shortboards_from_evidence_are_merged() -> None:
    board = MetricShortboard(
        code="threshold_slack_out_of_band",
        metric="threshold_slack",
        detail="slack 12 > band 10",
    )
    doc = _aggregate(perf_slack=_slack(value=12.0, shortboards=(board,)))
    assert any(b.code == "threshold_slack_out_of_band" for b in doc.shortboards)


def test_factory_hands_risk_facts_through_metrics_document_of() -> None:
    risk = resolve_risk_tier([_case(priority="P0", severity="blocker", risk={"level": "medium"})])
    doc = _aggregate(risk=risk)
    assert doc.risk_tier == risk.tier
    assert doc.risk_tier_lower_bound == risk.lower_bound
    assert doc.risk_tier_declared == risk.declared
    assert doc.risk_declaration_lowered == risk.declaration_lowered
    assert doc.risk_lowered_declarations == risk.lowered_declarations


def test_document_round_trips_through_json() -> None:
    doc = _aggregate()
    raw = json.loads(doc.model_dump_json())
    from assurance_agent.artifacts.models.metrics import MetricsDocument

    again = MetricsDocument.model_validate(raw)
    assert again.replay_subtree() == doc.replay_subtree()
