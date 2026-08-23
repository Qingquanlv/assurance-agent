from __future__ import annotations

import pytest

pytestmark = pytest.mark.usefixtures("product_runner")


def test_issue_path_runs_triage_analysis_and_fix_before_rerun(product_runner):
    trace = product_runner(execution_sequence=("failed", "passed")).run_to_report()
    assert trace.logical_steps_between("execution", "execution") == (
        "quality.issue-triage",
        "quality.issue-analysis",
        "healing.fix-proposal",
    )
    assert trace.report.exists


def test_product_issue_runs_the_same_healing_chain(product_runner):
    trace = product_runner(execution_sequence=("product_issue", "passed")).run_to_report()
    assert trace.logical_steps_between("execution", "execution") == (
        "quality.issue-triage",
        "quality.issue-analysis",
        "healing.fix-proposal",
    )
    assert "execution.run" in trace.logical_steps
    assert trace.report.exists
