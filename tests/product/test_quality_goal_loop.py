from __future__ import annotations

import asyncio

import pytest

from tests.product.goal_loop_fixture import GoalLoopScenario, run_goal_loop


def test_coverage_reenters_the_same_complete_case_flow(tmp_path):
    run = asyncio.run(run_goal_loop(tmp_path, GoalLoopScenario(coverage_values=(0.40, 1.0))))
    assert run.node_visits.count("prepare") == 1
    assert run.node_visits.count("case") == 2
    assert run.dispatch_count("intake.case-design") == 2
    assert run.dispatch_count("execution.execute") == 2
    assert run.dispatch_count("quality.inspect") == 2
    assert run.dispatch_count("quality.report") == 1
    assert run.node_visits.count("retro") == 0
    assert run.coverage_epochs == (0, 1)
    assert not any("coverage-repair" in name for name in run.task_dispatches)
    assert run.state["status"] == "completed"


def test_proposal_only_never_dispatches_rerun(tmp_path):
    scenario = GoalLoopScenario(execution_failures=1, proposal_only=True)
    run = asyncio.run(run_goal_loop(tmp_path, scenario))
    assert run.dispatch_count("execution.run") == 0
    assert run.node_visits.count("case") == 1
    assert run.state["status"] == "failed"


def test_failed_report_never_enters_normal_retro(tmp_path):
    run = asyncio.run(run_goal_loop(tmp_path, GoalLoopScenario(report_fails=True)))
    assert run.dispatch_count("quality.report") == 1
    assert "retro" not in run.node_visits
    assert run.state["status"] == "failed"


@pytest.mark.parametrize(
    ("scenario", "expected"),
    [
        (GoalLoopScenario(), "completed"),
        (GoalLoopScenario(coverage_values=(0.4,), coverage_rounds=0), "failed"),
        (GoalLoopScenario(execution_failures=1), "completed"),
        (GoalLoopScenario(execution_failures=1, healing_rounds=0), "failed"),
    ],
)
def test_goal_loop_terminal_matrix(tmp_path, scenario, expected):
    run = asyncio.run(run_goal_loop(tmp_path, scenario))
    assert run.state["status"] == expected
