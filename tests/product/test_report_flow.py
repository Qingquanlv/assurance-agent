"""Report publication on the real full flow."""

from __future__ import annotations

import asyncio

from graph_engine.testing.graph_harness import committed

from tests.product.test_execute_tail_flow import _SHA, _committed_issue
from tests.product.test_full_flow import (
    _RECEIPT,
    _case,
    _failure,
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


def test_report_is_mandatory_on_success(tmp_path) -> None:
    del tmp_path

    async def run() -> None:
        script: dict[str, list[object]] = {}
        _front(script)
        _case(script)
        _tail_until_inspect(script, ("satisfied", 0))
        _reported(script)
        done = await _invoke(script, budgets=_budgets())
        names = _names(done.captured)
        assert names.count("quality.report") == 1
        assert "improvement.retro" not in names
        assert done.result["status"] == "completed"  # type: ignore[index]

    asyncio.run(run())


def test_report_follows_successful_existing_test_repair(tmp_path) -> None:
    del tmp_path

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
        assert names.count("healing.apply-test-repair") == 1
        assert names.count("execution.run") == 1
        assert names.count("quality.inspect") == 2
        assert names.count("quality.report") == 1
        assert done.result["status"] == "completed"  # type: ignore[index]

    asyncio.run(run())


def test_nonrepairable_blocked_publishes_diagnostic_report(tmp_path) -> None:
    del tmp_path

    async def run() -> None:
        script: dict[str, list[object]] = {}
        _front(script)
        _case(script)
        _tail_until_inspect(script, ("blocked", 0))
        script["quality.issue-analyze"] = [_committed_issue("report_issue")]
        from tests.product.test_execute_tail_flow import _continue_diagnostic, _report_output

        script["quality.report"] = [committed(_report_output("diagnostic"), _RECEIPT)]
        _continue_diagnostic(script)
        done = await _invoke(script, budgets=_budgets())
        names = _names(done.captured)
        assert "healing.apply-test-repair" not in names
        assert names.count("quality.issue-analyze") == 1
        assert names.count("quality.report") == 1
        assert "improvement.retro-build-slices" in names
        assert done.outcome == "not_achieved"

    asyncio.run(run())


def test_needs_human_has_no_normal_report(tmp_path) -> None:
    del tmp_path

    async def run() -> None:
        script: dict[str, list[object]] = {}
        _front(script)
        _case(script)
        _tail_until_inspect(script, ("needs_human", 0))
        done = await _invoke(script, budgets=_budgets())
        names = _names(done.captured)
        assert "healing.apply-test-repair" not in names
        assert "quality.report" not in names
        assert "improvement.retro" not in names
        assert done.outcome == "not_achieved"

    asyncio.run(run())


def test_low_coverage_reenters_case_then_reports_once(tmp_path) -> None:
    del tmp_path

    async def run() -> None:
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
        names = _names(done.captured)
        assert names.count("intake.case-design") == 2
        assert names.count("quality.inspect") == 2
        assert names.count("quality.report") == 1
        assert not any("coverage-repair" in item for item in names)
        assert done.result["status"] == "completed"  # type: ignore[index]

    asyncio.run(run())


def test_coverage_exhaustion_has_no_report_or_retro(tmp_path) -> None:
    del tmp_path

    async def run() -> None:
        script: dict[str, list[object]] = {}
        _front(script)
        _case(script, "pass", "pass", epochs=(0, 1))
        _tail_until_inspect(script, ("coverage_insufficient", 0), ("coverage_insufficient", 1))
        script["intake.coverage-rework"] = [
            committed(
                {"rework_ref": {"path": "qa/results/cases/case-rework-context.json", "digest": _SHA}},
                _RECEIPT,
                artifacts=[{"path": "qa/results/cases/case-rework-context.json", "digest": _SHA}],
            )
        ]
        done = await _invoke(script, budgets=_budgets(coverage_rounds=1))
        names = _names(done.captured)
        assert names.count("intake.case-design") == 2
        assert "quality.report" not in names
        assert "improvement.retro" not in names
        assert done.outcome == "not_achieved"

    asyncio.run(run())


def test_failed_report_attempt_does_not_publish(tmp_path) -> None:
    del tmp_path

    async def run() -> None:
        script: dict[str, list[object]] = {}
        _front(script)
        _case(script)
        _tail_until_inspect(script, ("satisfied", 0))
        script["quality.report"] = [_failure()]
        done = await _invoke(script, budgets=_budgets())
        assert done.outcome == "not_achieved"
        assert "improvement.retro" not in _names(done.captured)

    asyncio.run(run())
