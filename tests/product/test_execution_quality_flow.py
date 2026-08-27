from __future__ import annotations

import pytest

pytestmark = pytest.mark.usefixtures("product_runner")


def test_success_path_runs_execution_quality_and_report(product_runner):
    trace = product_runner().run_to_report()
    assert "execution.execute" in trace.logical_steps
    assert "quality.fact-baseline" in trace.logical_steps
    assert "quality.inspect" in trace.logical_steps
    assert "quality.report" in trace.logical_steps
    assert trace.report.exists
    assert trace.status == "succeeded"


def test_test_failure_still_produces_a_report(product_runner):
    trace = product_runner(execution_sequence=("failed", "passed")).run_to_report()
    assert trace.report.exists
    assert "quality.report" in trace.logical_steps


def test_product_issue_still_produces_a_report(product_runner):
    trace = product_runner(execution_sequence=("product_issue", "passed")).run_to_report()
    assert trace.report.exists
    assert "quality.issue-triage" in trace.logical_steps
    assert "quality.report" in trace.logical_steps


def test_infrastructure_failure_stops_with_a_report(product_runner):
    trace = product_runner(execution_sequence=("infrastructure_failure",)).run_to_report()
    assert trace.status == "stopped"
    assert trace.report.exists
    assert "quality.report" in trace.logical_steps
    assert "quality.issue-triage" not in trace.logical_steps
