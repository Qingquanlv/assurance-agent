"""Goal-loop outcomes on the real full flow."""

from __future__ import annotations

import asyncio

from graph_engine.attempts.resolutions import PermanentTaskFailure
from graph_engine.testing.graph_harness import committed

from tests.product.test_execute_tail_flow import _SHA
from tests.product.test_full_flow import (
    _RECEIPT,
    _case,
    _front,
    _invoke,
    _one_repair,
    _reported,
    _tail_until_inspect,
)


def _names(captured: list[tuple[str, object]]) -> list[str]:
    return [name for name, _item in captured]


def _budgets(**overrides: int) -> dict[str, int]:
    values = {
        "review_rounds": 2,
        "coverage_rounds": 1,
        "healing_rounds": 1,
        "execution_retries": 1,
    }
    values.update(overrides)
    return values


async def _reentry() -> list[str]:
    script: dict[str, list[object]] = {}
    _front(script)
    _case(script, "pass", "pass", epochs=(0, 1))
    _tail_until_inspect(script, ("coverage_insufficient", 0), ("satisfied", 1))
    script["intake.coverage-rework"] = [
        committed(
            {"rework_ref": {"path": "qa/results/cases/case-rework-context.json", "digest": _SHA}},
            _RECEIPT,
            artifacts=[{"path": "qa/results/cases/case-rework-context.json", "digest": _SHA}],
        )
    ]
    _reported(script)
    done = await _invoke(script, budgets=_budgets())
    assert done.outcome == "achieved"
    return _names(done.captured)


def test_coverage_reenters_the_same_complete_case_flow(tmp_path) -> None:
    del tmp_path
    names = asyncio.run(_reentry())
    assert names.count("intake.intake") == 1
    assert names.count("intake.case-design") == 2
    assert names.count("execution.execute") == 2
    assert names.count("quality.inspect") == 2
    assert names.count("quality.report") == 1
    assert "improvement.retro" not in names
    assert not any("coverage-repair" in name for name in names)


def test_proposal_only_never_dispatches_rerun(tmp_path) -> None:
    del tmp_path
    from tests.product.test_issue_healing_flow import _proposal_does_not_rerun

    asyncio.run(_proposal_does_not_rerun())


def test_failed_report_never_enters_normal_retro(tmp_path) -> None:
    del tmp_path

    async def run() -> None:
        script: dict[str, list[object]] = {}
        _front(script)
        _case(script)
        _tail_until_inspect(script, ("satisfied", 0))
        script["quality.report"] = [PermanentTaskFailure(kind="invalid_output", message="no report")]
        done = await _invoke(script, budgets=_budgets())
        names = _names(done.captured)
        assert names.count("quality.report") == 1
        assert "improvement.retro" not in names
        assert done.outcome == "not_achieved"

    asyncio.run(run())


def test_goal_loop_reaches_achieved() -> None:
    names = asyncio.run(_reentry())
    assert "quality.report" in names


def test_goal_loop_exhausts_coverage_without_achievement() -> None:
    async def run() -> None:
        script: dict[str, list[object]] = {}
        _front(script)
        _case(script)
        _tail_until_inspect(script, ("coverage_insufficient", 0))
        done = await _invoke(script, budgets=_budgets(coverage_rounds=0))
        assert done.outcome == "not_achieved"
        assert "quality.report" not in _names(done.captured)

    asyncio.run(run())


def test_goal_loop_repair_then_achieved() -> None:
    async def run() -> None:
        script: dict[str, list[object]] = {}
        _front(script)
        _case(script)
        _one_repair(script, 0, "satisfied")
        _reported(script)
        done = await _invoke(
            script,
            budgets=_budgets(),
        )
        names = _names(done.captured)
        assert done.outcome == "achieved"
        assert names.count("healing.apply-test-repair") == 1
        assert names.count("execution.run") == 1

    asyncio.run(run())


def test_healing_budget_exhaustion_is_not_an_applied_repair() -> None:
    async def run() -> None:
        script: dict[str, list[object]] = {}
        _front(script)
        _case(script)
        _tail_until_inspect(script, ("repairable_execution_failure", 0))
        done = await _invoke(script, budgets=_budgets(healing_rounds=0))
        names = _names(done.captured)
        assert done.outcome == "not_achieved"
        assert "healing.apply-test-repair" not in names
        assert "execution.run" not in names

    asyncio.run(run())
