"""compute-threshold-slack — threshold/measured; >10 shortboard (Task 6 / §5-B5)."""

from __future__ import annotations

from assurance_agent.artifacts.models.inspect import PerformanceScenarioVerdict
from assurance_agent.workflow.metrics.threshold_slack import (
    DEFAULT_SLACK_BAND,
    compute_threshold_slack,
)

CHANGE_ID = "CH-B5-001"
BATCH = "20260805-100000"


def _scenario(*, threshold: float, measured: float | None) -> PerformanceScenarioVerdict:
    return PerformanceScenarioVerdict(
        capability="api_list",
        endpoint="/api/v1/dept/list",
        measured_p95_ms=measured,
        threshold_p95_ms=threshold,
        measured_error_rate=0.0,
        threshold_error_rate_max=0.01,
        verdict="PASS" if measured is not None else "SKIPPED",
    )


def test_slack_is_threshold_over_measured() -> None:
    evidence = compute_threshold_slack(
        change_id=CHANGE_ID,
        batch_id=BATCH,
        scenarios=(_scenario(threshold=2000.0, measured=500.0),),
    )
    assert evidence.value == 4.0
    assert evidence.shortboards == ()
    assert evidence.scenarios[0].slack == 4.0


def test_slack_above_default_band_produces_shortboard_only() -> None:
    evidence = compute_threshold_slack(
        change_id=CHANGE_ID,
        batch_id=BATCH,
        scenarios=(_scenario(threshold=2000.0, measured=8.0),),
    )
    assert evidence.value == 250.0
    assert evidence.slack_band == DEFAULT_SLACK_BAND
    assert len(evidence.shortboards) == 1
    assert evidence.shortboards[0].code == "threshold_slack_out_of_band"
    assert evidence.shortboards[0].metric == "threshold_slack"
    assert evidence.collection_gaps == ()


def test_missing_scenarios_is_typed_collection_gap() -> None:
    evidence = compute_threshold_slack(
        change_id=CHANGE_ID,
        batch_id=BATCH,
        scenarios=(),
    )
    assert evidence.value is None
    assert evidence.collection_gaps[0].code == "collection_failed"
    assert evidence.collection_gaps[0].metric == "threshold_slack"
