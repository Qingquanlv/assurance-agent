from __future__ import annotations

from assurance_agent.eval.gate import compute_gate_result
from assurance_agent.eval.types import (
    EvalSuite,
    RunManifest,
    SuiteMetrics,
    SuiteThreshold,
)


def _manifest(**kw: object) -> RunManifest:
    base: dict[str, object] = {
        "run_id": "run-1",
        "suite": "workflow-case",
        "scorer": "workflow_case",
        "selected_sample_ids": ["S-1"],
        "total_samples": 1,
        "executed_samples": 1,
        "target_model": "m",
        "started_at": "2026-07-15T00:00:00Z",
    }
    base.update(kw)
    return RunManifest.model_validate(base)


def _metrics(**metrics) -> SuiteMetrics:
    return SuiteMetrics(run_id="run-1", suite="workflow-case", sample_count=1, metrics=metrics)


_SUITE = EvalSuite(
    name="workflow-case",
    scorer="workflow_case",
    executor={},
    thresholds=[
        SuiteThreshold(metric="case_review_gate_pass_rate", gate="hard", op="gte", value=0.99),
        SuiteThreshold(metric="secret_leak_count", gate="hard", op="eq", value=0.0),
        SuiteThreshold(metric="target_file_coverage_rate", gate="advisory", op="gte", value=0.95),
    ],
)


def test_gate_pass_all_green() -> None:
    result = compute_gate_result(
        _SUITE,
        _manifest(),
        _metrics(case_review_gate_pass_rate=1.0, secret_leak_count=0.0, target_file_coverage_rate=1.0),
    )
    assert result.verdict == "pass"
    assert result.hard_gate_failures == []


def test_gate_hard_failure_is_fail() -> None:
    result = compute_gate_result(
        _SUITE, _manifest(), _metrics(case_review_gate_pass_rate=0.5, secret_leak_count=0.0)
    )
    assert result.verdict == "fail"
    assert "case_review_gate_pass_rate" in result.hard_gate_failures


def test_gate_advisory_only_is_pass_with_warnings() -> None:
    result = compute_gate_result(
        _SUITE,
        _manifest(),
        _metrics(case_review_gate_pass_rate=1.0, secret_leak_count=0.0, target_file_coverage_rate=0.5),
    )
    assert result.verdict == "pass_with_warnings"
    assert "target_file_coverage_rate" in result.warnings


def test_gate_evidence_integrity_mismatch_is_inconclusive() -> None:
    result = compute_gate_result(
        _SUITE, _manifest(executed_samples=0), _metrics(case_review_gate_pass_rate=1.0, secret_leak_count=0.0)
    )
    assert result.verdict == "inconclusive"
    assert any("evidence_integrity" in f for f in result.hard_gate_failures)


def test_gate_with_no_thresholds_is_inconclusive_when_a_sample_errored() -> None:
    suite = EvalSuite(name="workflow-full", scorer="workflow-full", executor={}, thresholds=[])
    metrics = SuiteMetrics(
        run_id="run-1",
        suite="workflow-full",
        sample_count=1,
        error_count=1,
    )

    result = compute_gate_result(suite, _manifest(suite="workflow-full"), metrics)

    assert result.verdict == "inconclusive"
    assert result.inconclusive_count == 1
    assert "sample_execution_errors: 1" in result.threshold_failures
