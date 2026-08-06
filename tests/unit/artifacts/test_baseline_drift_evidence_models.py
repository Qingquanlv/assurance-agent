"""Nightly baseline-drift evidence (M2 Task 5) for aggregate-nightly."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from assurance_agent.artifacts.models.metrics import MetricShortboard
from assurance_agent.artifacts.models.pr_metric_evidence import (
    BaselineDriftEvidence,
    BaselineDriftScenario,
)


def _scenario(
    *,
    capability: str = "login",
    endpoint: str = "POST /api/login",
    current_p95_ms: float = 12.0,
    baseline_p95_ms: float = 10.0,
    current_error_rate: float = 0.0,
    baseline_error_rate: float = 0.0,
    drift: float = 0.2,
    sample_count: int = 3,
) -> BaselineDriftScenario:
    return BaselineDriftScenario(
        capability=capability,
        endpoint=endpoint,
        current_p95_ms=current_p95_ms,
        baseline_p95_ms=baseline_p95_ms,
        current_error_rate=current_error_rate,
        baseline_error_rate=baseline_error_rate,
        drift=drift,
        sample_count=sample_count,
    )


def test_evaluated_publishes_scalar_value_and_scenarios() -> None:
    evidence = BaselineDriftEvidence(
        schema_version="1",
        change_id="CH-1",
        batch_id="nightly",
        status="evaluated",
        value=0.2,
        drift_band=0.2,
        window=5,
        scenarios=(_scenario(),),
        shortboards=(
            MetricShortboard(
                code="baseline_drift_out_of_band",
                metric="baseline_drift",
                detail="max drift 0.2 exceeds band 0.2",
            ),
        ),
    )
    assert evidence.value == 0.2
    assert evidence.scenarios[0].capability == "login"
    assert evidence.shortboards[0].code == "baseline_drift_out_of_band"


def test_not_evaluated_requires_sample_insufficient_and_no_value() -> None:
    evidence = BaselineDriftEvidence(
        schema_version="1",
        change_id="CH-1",
        batch_id="nightly",
        status="not_evaluated",
        value=None,
        drift_band=0.2,
        window=5,
        scenarios=(),
        shortboards=(
            MetricShortboard(
                code="sample_insufficient",
                metric="baseline_drift",
                detail="need 3 verified baseline samples",
            ),
        ),
    )
    assert evidence.value is None
    assert evidence.shortboards[0].code == "sample_insufficient"


def test_not_evaluated_refuses_missing_sample_insufficient() -> None:
    with pytest.raises(ValidationError, match="sample_insufficient"):
        BaselineDriftEvidence(
            schema_version="1",
            change_id="CH-1",
            batch_id="nightly",
            status="not_evaluated",
            value=None,
            drift_band=0.2,
            window=5,
        )


def test_evaluated_refuses_null_value() -> None:
    with pytest.raises(ValidationError, match="value"):
        BaselineDriftEvidence(
            schema_version="1",
            change_id="CH-1",
            batch_id="nightly",
            status="evaluated",
            value=None,
            drift_band=0.2,
            window=5,
            scenarios=(_scenario(),),
        )
