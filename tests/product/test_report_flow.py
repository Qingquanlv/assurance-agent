from __future__ import annotations

import pytest

from tests.product.execution_loop import bind_installed_sources, drive_coverage_loop

pytestmark = pytest.mark.usefixtures("product_runner", "installed_sources")


@pytest.fixture(autouse=True)
def _bind_sources(installed_sources) -> None:
    bind_installed_sources(installed_sources)


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
    assert trace.status in {"stopped", "succeeded"}
    assert "quality.report" in trace.logical_steps


def test_exhausted_and_inconclusive_reports_are_not_achieved() -> None:
    exhausted = drive_coverage_loop(
        coverage_states=("exhausted",),
        measured_sequence=(0.10,),
        coverage_rounds=0,
    )
    inconclusive = drive_coverage_loop(
        coverage_states=("inconclusive",),
        measured_sequence=(0.10,),
    )
    assert "quality.report" in exhausted.public_exports
    assert "quality.report" in inconclusive.public_exports
    assert exhausted.terminal != "achieved"
    assert inconclusive.terminal != "achieved"
    assert "healing.repair-coverage" not in exhausted.public_exports
    assert "healing.repair-coverage" not in inconclusive.public_exports
