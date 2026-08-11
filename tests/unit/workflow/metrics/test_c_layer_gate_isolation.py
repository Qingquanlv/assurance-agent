"""C-layer doc must not move metrics-sufficiency or quality_gate verdicts."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from assurance_agent.artifacts.canonical import canonical_json_bytes
from assurance_agent.artifacts.models import CoverageThreshold
from assurance_agent.artifacts.models.c_layer import (
    C_LAYER_METRICS_REL,
    CLayerMetricEntry,
    CLayerMetricsDocument,
)
from assurance_agent.artifacts.models.metrics import METRIC_KEYS, MetricEntry, MetricScope, MetricsDocument
from assurance_agent.artifacts.policy import load_policy_bytes
from assurance_agent.evidence.metrics_sufficiency import evaluate_metrics_sufficiency
from assurance_agent.workflow.execution.results import (
    CaseResult,
    CoverageResult,
    ResultSource,
    TargetResult,
)
from assurance_agent.workflow.report.quality_gate import build_quality_gate
from tests.helpers_aa import sufficient_evidence_coverage


def _metrics_doc() -> MetricsDocument:
    return MetricsDocument.model_validate(
        {
            "schema_version": "2",
            "change_id": "CH-1",
            "cadence": "pr",
            "computed_at": datetime(2026, 8, 5, 2, 0, tzinfo=UTC),
            "risk_tier": "low",
            "risk_tier_lower_bound": "low",
            "risk_tier_declared": None,
            "risk_declaration_lowered": False,
            "risk_lowered_declarations": (),
            "metrics": {
                "constraint_coverage": MetricEntry(
                    layer="api",
                    status="evaluated",
                    value=0.9,
                    declared=MetricScope.of(total=10, covered=9),
                    evidence="constraint-coverage.json",
                )
            },
            "policy_digest": "0" * 64,
        }
    )


def _c_layer_doc() -> CLayerMetricsDocument:
    pending = CLayerMetricEntry(status="not_evaluated")
    return CLayerMetricsDocument(
        schema_version="1",
        change_id="CH-1",
        cadence="report",
        computed_at=datetime(2026, 8, 5, 2, 0, tzinfo=UTC),
        escape_rate=CLayerMetricEntry(
            status="evaluated",
            numerator=9,
            denominator=10,
            rate=0.9,
        ),
        counterexample_promotion_rate=pending,
        coverage_gap_closure_rate=pending,
        seed_replay_stability=CLayerMetricEntry(
            status="evaluated",
            numerator=0,
            denominator=1,
            rate=0.0,
        ),
    )


def _api() -> TargetResult:
    return TargetResult(
        change_id="CH-1",
        batch_id="b1",
        target="api",
        status="passed",
        command="cmd",
        source=ResultSource(framework="pytest", raw_log="raw/x.log"),
        total=1,
        passed=1,
        failed=0,
        skipped=0,
        cases=[
            CaseResult(
                case_id="TC_API_001",
                status="passed",
                file="f.py",
                test_name="t",
                duration_ms=1,
                message="",
            )
        ],
        unmapped_tests=[],
    )


def _coverage() -> CoverageResult:
    return CoverageResult(
        change_id="CH-1",
        batch_id="b1",
        available=True,
        line_coverage=85.0,
        branch_coverage=70.0,
        threshold=CoverageThreshold(line=70, branch=60),
        status="PASS",
    )


def test_metrics_sufficiency_unaffected_by_c_layer_artifact(tmp_path: Path) -> None:
    change_dir = tmp_path / "qa" / "changes" / "CH-1"
    (change_dir / "inspect").mkdir(parents=True)
    (change_dir / C_LAYER_METRICS_REL).write_bytes(canonical_json_bytes(_c_layer_doc()))

    policy = load_policy_bytes(None, origin="packaged")
    decision = evaluate_metrics_sufficiency(_metrics_doc(), policy.evidence_sufficiency)
    assert decision.verdict == "pass"
    # Packaged floors stay MetricKey-closed — no C-layer keys.
    for band in policy.evidence_sufficiency.floors.values():
        assert set(band).issubset(set(METRIC_KEYS))
        assert "escape_rate" not in band
        assert "counterexample_promotion_rate" not in band
        assert "coverage_gap_closure_rate" not in band
        assert "seed_replay_stability" not in band
    assert (change_dir / C_LAYER_METRICS_REL).is_file()


def test_quality_gate_final_status_unchanged_when_c_layer_present(tmp_path: Path) -> None:
    change_dir = tmp_path / "qa" / "changes" / "CH-1"
    (change_dir / "inspect").mkdir(parents=True)
    (change_dir / C_LAYER_METRICS_REL).write_bytes(canonical_json_bytes(_c_layer_doc()))

    without = build_quality_gate(
        change_id="CH-1",
        batch_id="b1",
        api=_api(),
        e2e=None,
        coverage=_coverage(),
        evidence_coverage=sufficient_evidence_coverage(),
    )
    with_doc_present = build_quality_gate(
        change_id="CH-1",
        batch_id="b1",
        api=_api(),
        e2e=None,
        coverage=_coverage(),
        evidence_coverage=sufficient_evidence_coverage(),
    )
    assert without.final_status == with_doc_present.final_status == "PASS"
    assert not hasattr(without.dimensions, "c_layer")
    assert "metrics-c-layer" not in without.model_dump_json()
    assert (change_dir / C_LAYER_METRICS_REL).is_file()
