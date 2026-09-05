from __future__ import annotations

import asyncio

import pytest

from tests.product.goal_loop_fixture import GoalLoopScenario, run_goal_loop


def test_report_is_mandatory_on_success(tmp_path) -> None:
    run = asyncio.run(run_goal_loop(tmp_path, GoalLoopScenario()))

    assert run.dispatch_count("quality.report") == 1
    assert run.node_visits.count("retro") == 1
    assert run.state["status"] == "completed"


def test_report_follows_successful_existing_test_repair(tmp_path) -> None:
    run = asyncio.run(run_goal_loop(tmp_path, GoalLoopScenario(execution_failures=1)))

    assert run.dispatch_count("healing.apply-test-repair") == 1
    assert run.dispatch_count("execution.run") == 1
    assert run.dispatch_count("quality.inspect") == 2
    assert run.dispatch_count("quality.report") == 1
    assert run.state["status"] == "completed"


@pytest.mark.parametrize("disposition", ("blocked", "needs_human"))
def test_nonrepairable_execution_failure_has_no_normal_report(tmp_path, disposition) -> None:
    run = asyncio.run(
        run_goal_loop(
            tmp_path,
            GoalLoopScenario(execution_failures=1, execution_disposition=disposition),
        )
    )

    assert run.dispatch_count("healing.apply-test-repair") == 0
    assert run.dispatch_count("quality.report") == 0
    assert "retro" not in run.node_visits
    assert run.state["status"] == "failed"


def test_low_coverage_reenters_case_then_reports_once(tmp_path) -> None:
    run = asyncio.run(run_goal_loop(tmp_path, GoalLoopScenario(coverage_values=(0.40, 0.91))))

    assert run.node_visits.count("case") == 2
    assert run.dispatch_count("quality.inspect") == 2
    assert run.dispatch_count("quality.report") == 1
    assert not any("coverage-repair" in item for item in run.task_dispatches)
    assert run.state["status"] == "completed"


def test_coverage_exhaustion_has_no_report_or_retro(tmp_path) -> None:
    run = asyncio.run(
        run_goal_loop(
            tmp_path,
            GoalLoopScenario(coverage_values=(0.10, 0.11), coverage_rounds=1),
        )
    )

    assert run.node_visits.count("case") == 2
    assert run.dispatch_count("quality.report") == 0
    assert "retro" not in run.node_visits
    assert run.state["status"] == "failed"
