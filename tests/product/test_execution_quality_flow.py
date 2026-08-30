from __future__ import annotations

import pytest

from tests.product.execution_loop import (
    bind_installed_sources,
    drive_execution_loop,
    drive_failed_execution,
)

pytestmark = pytest.mark.usefixtures("installed_sources")


@pytest.fixture(autouse=True)
def _bind_sources(installed_sources) -> None:
    bind_installed_sources(installed_sources)


def test_passed_verdict_reaches_quality_assessment_not_healing() -> None:
    trace = drive_execution_loop(
        execution_sequence=("passed",),
        classifications=(),
        fix_eligible=(),
    )
    assert "quality.assess" in trace.public_exports
    assert "quality.issue-analyze" not in trace.public_exports
    assert "healing.repair-failure" not in trace.public_exports


def test_fix_eligible_test_failure_enters_healing() -> None:
    trace = drive_failed_execution(
        classification="test", fix_eligible=True, execution_sequence=("failed", "passed")
    )
    assert "quality.issue-analyze" in trace.public_exports
    assert "healing.repair-failure" in trace.public_exports


@pytest.mark.parametrize("classification", ["product_bug", "environment_failure", "infrastructure_failure"])
def test_non_test_failure_never_dispatches_test_fix(classification: str) -> None:
    trace = drive_failed_execution(classification=classification, fix_eligible=False)
    assert "healing.repair-failure" not in trace.public_exports
    assert trace.terminal != "achieved"


@pytest.mark.parametrize("classification", ["unknown", "pending", "failed"])
def test_unknown_pending_failed_analysis_is_fail_closed(classification: str) -> None:
    trace = drive_failed_execution(classification=classification, fix_eligible=False)
    assert "healing.repair-failure" not in trace.public_exports
    assert trace.terminal != "achieved"


def test_success_path_runs_execution_quality_and_report() -> None:
    trace = drive_execution_loop(
        execution_sequence=("passed",),
        classifications=(),
        fix_eligible=(),
    )
    assert "execution.execute" in trace.public_exports
    assert "quality.assess" in trace.public_exports
    assert "quality.report" in trace.public_exports
    assert trace.status == "succeeded"


def test_test_failure_still_produces_a_report() -> None:
    trace = drive_failed_execution(
        classification="test",
        fix_eligible=True,
        execution_sequence=("failed", "passed"),
    )
    assert "quality.report" in trace.public_exports


def test_product_issue_still_produces_a_report() -> None:
    trace = drive_failed_execution(classification="product_bug", fix_eligible=False)
    assert "quality.issue-analyze" in trace.public_exports
    assert "quality.report" in trace.public_exports
    assert "healing.repair-failure" not in trace.public_exports


def test_infrastructure_failure_stops_with_a_report() -> None:
    trace = drive_failed_execution(classification="infrastructure_failure", fix_eligible=False)
    assert "quality.report" in trace.public_exports
    assert "healing.repair-failure" not in trace.public_exports
    assert trace.terminal != "achieved"
