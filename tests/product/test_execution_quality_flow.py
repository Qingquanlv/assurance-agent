"""Quality routes on the real execute-tail and full flows."""

from __future__ import annotations

import asyncio

from graph_engine.attempts.resolutions import PermanentTaskFailure
from graph_engine.testing.graph_harness import committed

from tests.product.test_execute_tail_flow import (
    _RECEIPT,
    _inspect_output,
    _invoke,
    _continue_diagnostic,
    _committed_issue,
    _materialize,
    _report_output,
    _through_execute,
)
from tests.product.test_full_flow import _case, _front, _inspect_commit, _invoke as _invoke_full
from tests.product.test_full_flow import _tail_until_inspect


def _names(captured: list[tuple[str, object]]) -> list[str]:
    return [name for name, _item in captured]


def test_assertion_analysis_product_bug_reaches_diagnostic_not_repair() -> None:
    asyncio.run(_diagnostic("report_issue"))


def test_assertion_analysis_test_bug_reaches_repair_rerun_and_inspect() -> None:
    from tests.product.test_issue_healing_flow import _applied_repair_reruns

    asyncio.run(_applied_repair_reruns())


def test_reported_inspect_does_not_enter_retro() -> None:
    async def run() -> None:
        script = _through_execute()
        script["quality.materialize-assessment-inputs"] = [_materialize()]
        script["quality.inspect"] = [_inspect_commit(_inspect_output("satisfied"))]
        script["quality.report"] = [committed(_report_output("reported"), _RECEIPT)]
        done = await _invoke(script)
        assert done.outcome == "reported"
        assert "improvement.retro" not in _names(done.captured)
        assert "quality.report" in _names(done.captured)

    asyncio.run(run())


def test_passed_execution_reaches_inspect_and_committed_report() -> None:
    async def run() -> None:
        script = _through_execute()
        script["quality.materialize-assessment-inputs"] = [_materialize()]
        script["quality.inspect"] = [_inspect_commit(_inspect_output("satisfied"))]
        script["quality.report"] = [committed(_report_output("reported"), _RECEIPT)]
        done = await _invoke(script)
        names = _names(done.captured)
        assert "execution.execute" in names
        assert "quality.inspect" in names
        assert "quality.report" in names
        assert done.outcome == "reported"

    asyncio.run(run())


def test_coverage_insufficient_returns_without_report() -> None:
    async def run() -> None:
        script = _through_execute()
        script["quality.materialize-assessment-inputs"] = [_materialize()]
        script["quality.inspect"] = [_inspect_commit(_inspect_output("coverage_insufficient"))]
        done = await _invoke(script)
        assert done.outcome == "coverage_insufficient"
        assert "quality.report" not in _names(done.captured)

    asyncio.run(run())


def test_needs_human_returns_without_test_repair_or_report() -> None:
    async def run() -> None:
        script = _through_execute()
        script["quality.materialize-assessment-inputs"] = [_materialize()]
        script["quality.inspect"] = [_inspect_commit(_inspect_output("needs_human"))]
        done = await _invoke(script)
        names = _names(done.captured)
        assert done.outcome == "needs_human"
        assert "healing.apply-test-repair" not in names
        assert "quality.report" not in names

    asyncio.run(run())


def test_invalid_execution_result_blocks_before_inspect() -> None:
    async def run() -> None:
        script = _through_execute()
        script["execution.execute"] = [PermanentTaskFailure(kind="invalid_output", message="bad execution")]
        done = await _invoke(script)
        names = _names(done.captured)
        assert done.outcome == "blocked"
        assert "quality.inspect" not in names

    asyncio.run(run())


def test_failed_inspect_attempt_stops_without_diagnostic_report() -> None:
    async def run() -> None:
        script = _through_execute()
        script["quality.materialize-assessment-inputs"] = [_materialize()]
        script["quality.inspect"] = [PermanentTaskFailure(kind="invalid_output", message="inspect failed")]
        done = await _invoke(script)
        assert done.outcome == "blocked"
        assert "quality.report" not in _names(done.captured)

    asyncio.run(run())


def test_failed_report_keeps_the_execution_checkpoint() -> None:
    async def run() -> None:
        script: dict[str, list[object]] = {}
        _front(script)
        _case(script)
        _tail_until_inspect(script, ("satisfied", 0))
        script["quality.report"] = [PermanentTaskFailure(kind="invalid_output", message="no report")]
        done = await _invoke_full(script)
        names = _names(done.captured)
        assert "execution.execute" in names
        assert names.count("quality.report") == 1
        assert done.outcome == "not_achieved"
        assert isinstance(done.result, dict)

    asyncio.run(run())


def test_blocking_inspection_publishes_diagnostic_report_without_achievement() -> None:
    asyncio.run(_diagnostic("report_issue"))


async def _diagnostic(route: str) -> None:
    script = _through_execute()
    script["quality.materialize-assessment-inputs"] = [_materialize()]
    script["quality.inspect"] = [_inspect_commit(_inspect_output("analysis_required"))]
    script["quality.issue-analyze"] = [_committed_issue(route)]
    script["quality.report"] = [committed(_report_output("diagnostic"), _RECEIPT)]
    _continue_diagnostic(script)
    done = await _invoke(script)
    names = _names(done.captured)
    assert "healing.fix-proposal" not in names
    assert "healing.apply-test-repair" not in names
    assert "quality.report" in names
    assert done.outcome == "diagnostic"
