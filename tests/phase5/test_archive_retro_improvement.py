from __future__ import annotations

import pytest

pytestmark = pytest.mark.usefixtures("product_runner")


def test_full_ends_at_achieved_without_archive(product_runner):
    result = product_runner(entrypoint="full").run_to_terminal()
    assert result.status == "completed"
    assert result.terminal_tail == ("quality.report",)
    assert "improvement.archive" not in result.logical_steps
    assert "improvement.retro" not in result.logical_steps
    assert "improvement.improvement-review" not in result.logical_steps


def test_archive_remains_a_separate_entrypoint(product_runner):
    archived = product_runner(entrypoint="archive").run_to_terminal()
    assert archived.status == "completed"
    assert "improvement.archive" in archived.logical_steps
    assert "quality.report" not in archived.logical_steps


def test_retro_and_improvement_are_independent_entrypoints(product_runner):
    retro = product_runner(entrypoint="retro").run_to_terminal()
    improvement = product_runner(entrypoint="improvement-apply").run_to_terminal()
    assert retro.logical_steps == (
        "improvement.retro",
        "improvement.retro-eval-analysis",
        "improvement.retro-issue-analysis",
        "improvement.retro-workflow-analysis",
    )
    assert improvement.logical_steps == ("improvement.improvement-review", "improvement.apply")
    assert retro.status == "completed"
    assert improvement.status == "completed"


def test_improvement_evaluate_export_and_rollback_are_independent(product_runner):
    evaluate = product_runner(entrypoint="improvement-evaluate").run_to_terminal()
    export = product_runner(entrypoint="improvement-export").run_to_terminal()
    rollback = product_runner(entrypoint="improvement-rollback").run_to_terminal()
    review = product_runner(entrypoint="improvement-review").run_to_terminal()
    assert evaluate.status == "completed"
    assert export.status == "completed"
    assert rollback.status == "completed"
    assert review.status == "completed"
    assert "improvement.evaluate" in evaluate.logical_steps
    assert "improvement.export" in export.logical_steps
    assert "improvement.rollback" in rollback.logical_steps
    assert review.logical_steps == ("improvement.improvement-review",)
    assert "quality.report" not in evaluate.logical_steps
    assert "quality.report" not in export.logical_steps
    assert "quality.report" not in rollback.logical_steps
    assert "quality.report" not in review.logical_steps
