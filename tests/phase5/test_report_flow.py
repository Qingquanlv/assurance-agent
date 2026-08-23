from __future__ import annotations

import pytest

pytestmark = pytest.mark.usefixtures("product_runner")


def test_report_is_mandatory_on_success(product_runner):
    trace = product_runner().run_to_report()
    assert trace.report.exists
    assert "quality.report" in trace.logical_steps
    assert trace.status == "succeeded"


def test_report_on_test_failure(product_runner):
    trace = product_runner(execution_sequence=("failed", "passed")).run_to_report()
    assert trace.report.exists


def test_report_on_product_issue(product_runner):
    trace = product_runner(execution_sequence=("product_issue", "passed")).run_to_report()
    assert trace.report.exists


def test_report_on_infrastructure_failure(product_runner):
    trace = product_runner(execution_sequence=("infrastructure_failure",)).run_to_report()
    assert trace.report.exists
    assert trace.status == "stopped"


def test_report_on_low_coverage_then_pass(product_runner):
    trace = product_runner(coverage_sequence=(0.40, 0.91), threshold=0.90).run_to_report()
    assert trace.report.exists
    assert trace.report.coverage == 0.91
    assert trace.status == "succeeded"


def test_report_on_repair_success(product_runner):
    trace = product_runner(coverage_sequence=(0.50, 0.95), threshold=0.90).run_to_report()
    assert trace.activations("assurance.healing.coverage-repair") == 1
    assert trace.report.exists
    assert trace.status == "succeeded"


def test_report_on_repair_exhaustion(product_runner):
    trace = product_runner(
        coverage_sequence=(0.10, 0.11),
        threshold=0.90,
        coverage_rounds=1,
    ).run_to_report()
    assert trace.report.exists
    assert trace.status == "stopped"
