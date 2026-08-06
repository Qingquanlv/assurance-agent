"""PR cadence batch evidence models (Verification Metrics M1 Task 6)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from assurance_agent.artifacts.models.metrics import MetricCollectionGap, MetricScope
from assurance_agent.artifacts.models.pr_metric_evidence import (
    AuthMatrixEvidence,
    ConstraintCoverageEvidence,
    CoverageDiffEvidence,
    JourneyCoverageEvidence,
    PerfSlackEvidence,
)


def test_coverage_diff_rejects_value_without_changed_lines() -> None:
    with pytest.raises(ValidationError, match="no changed lines"):
        CoverageDiffEvidence(
            schema_version="1",
            change_id="CH-1",
            batch_id="b1",
            total_changed_lines=0,
            covered_changed_lines=0,
            value=1.0,
        )


def test_coverage_diff_accepts_ratio_that_matches_counts() -> None:
    evidence = CoverageDiffEvidence(
        schema_version="1",
        change_id="CH-1",
        batch_id="b1",
        total_changed_lines=4,
        covered_changed_lines=2,
        value=0.5,
    )
    assert evidence.value == 0.5


def test_constraint_coverage_carries_entity_without_constraints_gap() -> None:
    evidence = ConstraintCoverageEvidence(
        schema_version="1",
        change_id="CH-1",
        batch_id="b1",
        declared=MetricScope.of(total=2, covered=1, uncovered=("entities.dept.constraints.x",)),
        value=0.5,
        collection_gaps=(
            MetricCollectionGap(
                code="entity_without_constraints",
                metric="constraint_coverage",
                subject="orphan",
            ),
        ),
    )
    assert evidence.collection_gaps[0].subject == "orphan"


def test_auth_matrix_empty_declared_allows_whole_metric_gap() -> None:
    evidence = AuthMatrixEvidence(
        schema_version="1",
        change_id="CH-1",
        batch_id="b1",
        declared=None,
        collection_gaps=(
            MetricCollectionGap(
                code="collection_failed",
                metric="auth_matrix_coverage",
                detail="empty",
            ),
        ),
    )
    assert evidence.value is None


def test_journey_and_slack_models_round_trip() -> None:
    journey = JourneyCoverageEvidence(
        schema_version="1",
        change_id="CH-1",
        batch_id="b1",
        declared=MetricScope.of(total=1, covered=0, uncovered=("j1",)),
        value=0.0,
    )
    slack = PerfSlackEvidence(
        schema_version="1",
        change_id="CH-1",
        batch_id="b1",
        value=4.0,
        slack_band=10.0,
    )
    assert journey.declared is not None and journey.declared.total == 1
    assert slack.value == 4.0
