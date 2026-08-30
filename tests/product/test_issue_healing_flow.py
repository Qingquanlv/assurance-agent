from __future__ import annotations

import pytest

from tests.product.execution_loop import (
    assert_each_repair_is_preceded_by_one_advance,
    bind_installed_sources,
    drive_execution_loop,
    drive_failed_execution,
)

pytestmark = pytest.mark.usefixtures("installed_sources")


@pytest.fixture(autouse=True)
def _bind_sources(installed_sources) -> None:
    bind_installed_sources(installed_sources)


def test_issue_path_runs_analysis_and_fix_before_rerun() -> None:
    trace = drive_failed_execution(
        classification="test",
        fix_eligible=True,
        execution_sequence=("failed", "passed"),
    )
    assert "quality.issue-analyze" in trace.public_exports
    assert "healing.repair-failure" in trace.public_exports
    assert "execution.rerun" in trace.public_exports
    assert_each_repair_is_preceded_by_one_advance(trace.task_capabilities)
    assert trace.advance_outputs == ({"kind": "failure", "rounds_used": 1, "rounds_budget": 1},)


def test_fix_eligible_test_data_failure_enters_healing() -> None:
    trace = drive_failed_execution(
        classification="test-data",
        fix_eligible=True,
        execution_sequence=("failed", "passed"),
    )
    assert "healing.repair-failure" in trace.public_exports
    assert_each_repair_is_preceded_by_one_advance(trace.task_capabilities)


def test_product_issue_never_runs_the_healing_chain() -> None:
    trace = drive_failed_execution(classification="product_bug", fix_eligible=False)
    assert "quality.issue-analyze" in trace.public_exports
    assert "healing.repair-failure" not in trace.public_exports
    assert "execution.rerun" not in trace.public_exports
    assert trace.advance_outputs == ()


def test_healed_rerun_passed_reaches_quality() -> None:
    trace = drive_failed_execution(
        classification="test",
        fix_eligible=True,
        execution_sequence=("failed", "passed"),
    )
    assert "execution.rerun" in trace.public_exports
    assert "quality.assess" in trace.public_exports
    assert trace.advance_outputs[0]["rounds_used"] == 1


def test_healed_rerun_still_failed_is_classified_again() -> None:
    trace = drive_execution_loop(
        execution_sequence=("failed", "failed"),
        classifications=("test", "test"),
        fix_eligible=(True, True),
        healing_rounds=2,
    )
    assert trace.public_exports.count("quality.issue-analyze") >= 1 or "quality.issue-analyze" in (
        trace.public_exports
    )
    analyze_count = sum(1 for item in trace.task_capabilities if item.endswith("issue-analysis.finalize"))
    assert analyze_count >= 2
    assert_each_repair_is_preceded_by_one_advance(trace.task_capabilities)
    assert tuple(item["rounds_used"] for item in trace.advance_outputs) == (1, 2)
    assert all(item["rounds_budget"] == 2 for item in trace.advance_outputs)


def test_failed_rerun_does_not_reset_the_counter() -> None:
    trace = drive_execution_loop(
        execution_sequence=("failed", "failed"),
        classifications=("test", "test"),
        fix_eligible=(True, True),
        healing_rounds=2,
    )
    assert tuple(item["rounds_used"] for item in trace.advance_outputs) == (1, 2)
    assert all(item["kind"] == "failure" for item in trace.advance_outputs)


def test_healing_budget_exhausted_terminates_before_another_repair() -> None:
    trace = drive_execution_loop(
        execution_sequence=("failed", "failed"),
        classifications=("test", "test"),
        fix_eligible=(True, True),
        healing_rounds=1,
    )
    assert_each_repair_is_preceded_by_one_advance(trace.task_capabilities)
    assert tuple(item["rounds_used"] for item in trace.advance_outputs) == (1,)
    assert trace.terminal != "achieved"
    repair_count = sum(1 for item in trace.task_capabilities if item.endswith("fix-proposal.finalize"))
    assert repair_count == 1
