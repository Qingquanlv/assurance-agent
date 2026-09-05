from __future__ import annotations

import asyncio

import pytest

from tests.product.goal_loop_fixture import GoalLoopScenario, resume_goal_loop, run_goal_loop


@pytest.mark.parametrize(
    "cut",
    (
        "after_coverage_advance",
        "after_application_commit",
        "after_inspect_commit",
    ),
)
def test_resume_preserves_business_identity_and_committed_writes(tmp_path, cut) -> None:
    scenario = GoalLoopScenario(coverage_values=(0.40, 1.0), execution_failures=1)

    asyncio.run(run_goal_loop(tmp_path, scenario, crash_at=cut))
    resumed = asyncio.run(resume_goal_loop(tmp_path))

    assert resumed.state["status"] == "completed"
    assert resumed.coverage_epochs == (0, 1)
    assert resumed.node_visits.count("prepare") == 1
    assert resumed.dispatch_count("healing.apply-test-repair") == 1
    assert resumed.dispatch_count("execution.run") == 1
    assert resumed.dispatch_count("quality.inspect") == 3
    assert resumed.dispatch_count("quality.report") == 1
    assert resumed.node_visits.count("retro") == 0


def test_new_epoch_uses_distinct_case_execution_and_inspect_keys_for_same_case_bytes(tmp_path) -> None:
    scenario = GoalLoopScenario(
        coverage_values=(0.40, 1.0),
        execution_failures=1,
        unchanged_case_bytes=True,
    )

    asyncio.run(run_goal_loop(tmp_path, scenario, crash_at="after_coverage_advance"))
    resumed = asyncio.run(resume_goal_loop(tmp_path))

    for semantic_id in ("intake.case-design", "execution.execute", "quality.inspect"):
        keys = resumed.attempt_keys[semantic_id]
        assert len(keys) == len(set(keys))
    assert resumed.attempt_keys["intake.case-design"] == (
        "intake.case-design:0",
        "intake.case-design:1",
    )


def test_resume_runs_in_a_fresh_process_and_reuses_the_sqlite_checkpoint(tmp_path) -> None:
    scenario = GoalLoopScenario(coverage_values=(0.40, 1.0))

    interrupted = asyncio.run(run_goal_loop(tmp_path, scenario, crash_at="after_coverage_advance"))
    assert interrupted.state["coverage_epoch"] == 1
    assert interrupted.node_visits.count("prepare") == 1

    resumed = asyncio.run(resume_goal_loop(tmp_path))

    assert resumed.state["status"] == "completed"
    assert resumed.node_visits.count("prepare") == 1
    assert any(path.name == "goal-loop-checkpoints.sqlite" for path in resumed.artifact_paths)
