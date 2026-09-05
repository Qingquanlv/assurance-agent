from __future__ import annotations

import asyncio
from typing import cast

from assurance_intake.contracts.plan import ResolvedAssurancePlan, decode_plan
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1
from tests.product.goal_loop_fixture import GoalLoopRun, GoalLoopScenario, run_goal_loop


def _loaded_plan(run: GoalLoopRun) -> ResolvedAssurancePlan:
    ref = EvidenceArtifactRefV1.model_validate(run.state["plan_ref"])
    data = next(path for path in run.artifact_paths if path.as_posix().endswith(ref.path)).read_bytes()
    return decode_plan(data, ref)


def test_one_plan_survives_two_coverage_epochs(tmp_path) -> None:
    scenario = GoalLoopScenario(
        coverage_values=(0.4, 1.0),
        candidate_families=("api", "e2e"),
        proposed_families=("api",),
    )

    run = asyncio.run(run_goal_loop(tmp_path, scenario))

    assert run.node_visits.count("prepare") == 1
    assert run.dispatch_count("intake.resolve-plan") == 1
    assert run.dispatch_count("intake.load-plan") == 0
    assert run.node_visits.count("case") == 2
    assert tuple(cast(tuple[str, ...], run.state["selected_test_families"])) == ("api",)
    assert run.coverage_epochs == (0, 1)
    assert run.state["terminal"] == {"status": "completed", "reason": "achieved"}
    assert "retro" not in run.node_visits

    plan = _loaded_plan(run)
    assert plan.plan_digest == run.state["plan_digest"]
    assert plan.selected_test_families == ("api",)
    assert cast(dict[str, object], run.state["reviewed_case"])["plan_digest"] == plan.plan_digest
    assert cast(dict[str, object], run.state["generation_result"])["plan_digest"] == plan.plan_digest
    assert cast(dict[str, object], run.state["execution_result"])["plan_digest"] == plan.plan_digest
    assert cast(dict[str, object], run.state["inspection_outcome"])["plan_digest"] == plan.plan_digest


def test_all_false_recommendations_fall_back_to_all_candidates(tmp_path) -> None:
    scenario = GoalLoopScenario(
        candidate_families=("api", "e2e"),
        proposed_families=(),
    )

    run = asyncio.run(run_goal_loop(tmp_path, scenario))

    plan = _loaded_plan(run)
    assert plan.selected_test_families == ("api", "e2e")
    assert tuple(cast(tuple[str, ...], run.state["selected_test_families"])) == plan.selected_test_families
    assert any(reason.reason_code == "fallback_all_candidates" for reason in plan.resolution_reasons)


def test_execute_imports_the_plan_without_resolving_it(tmp_path) -> None:
    scenario = GoalLoopScenario(
        entrypoint="execute",
        candidate_families=("api", "e2e"),
        proposed_families=("api",),
    )

    run = asyncio.run(run_goal_loop(tmp_path, scenario))

    assert run.dispatch_count("intake.resolve-plan") == 0
    assert run.dispatch_count("intake.load-plan") == 1
    assert run.node_visits.count("prepare") == 0
    assert tuple(cast(tuple[str, ...], run.state["selected_test_families"])) == ("api",)
