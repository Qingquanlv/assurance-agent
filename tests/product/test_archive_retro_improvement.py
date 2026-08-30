from __future__ import annotations

from pathlib import Path

import pytest

from tests.product.product_runner import ProductRun
from tests.product.test_workflow_modularization_golden import _modular_composition

pytestmark = pytest.mark.usefixtures("installed_sources")


def _run(installed_sources, tmp_path: Path, entrypoint: str, **kwargs):
    return ProductRun(
        entrypoint=entrypoint,
        selected_test_families=("api",) if entrypoint in {"full", "execute"} else (),
        review_decision=str(kwargs.get("review_decision", "pass")),
        healing_decision=str(kwargs.get("healing_decision", "allowed")),
        execution_sequence=tuple(kwargs.get("execution_sequence", ())),
        coverage_sequence=tuple(kwargs.get("coverage_sequence", ())),
        threshold=float(kwargs.get("threshold", 0.90)),
        coverage_rounds=kwargs.get("coverage_rounds"),
        engine_root=tmp_path / entrypoint,
        composition=_modular_composition(installed_sources),
    ).run_to_terminal()


def test_full_ends_at_achieved_without_archive(installed_sources, tmp_path: Path):
    result = _run(installed_sources, tmp_path, "full")
    assert result.status == "completed"
    assert result.change.state == "achieved"
    assert "improvement.archive" not in result.logical_steps
    assert "improvement.retro" in result.logical_steps
    assert "improvement.improvement-review" in result.logical_steps
    assert result.terminal_tail == (
        "quality.report",
        "improvement.retro",
        "improvement.retro-eval-analysis",
        "improvement.retro-issue-analysis",
        "improvement.retro-workflow-analysis",
        "improvement.improvement-review",
        "improvement.evaluate",
        "improvement.apply",
    )


def test_archive_remains_a_separate_entrypoint(installed_sources, tmp_path: Path):
    archived = _run(installed_sources, tmp_path, "archive")
    assert archived.status == "completed"
    assert "improvement.archive" in archived.logical_steps
    assert "quality.report" not in archived.logical_steps


def test_retro_and_improvement_are_independent_entrypoints(installed_sources, tmp_path: Path):
    retro = _run(installed_sources, tmp_path, "retro")
    improvement = _run(installed_sources, tmp_path, "improvement-apply")
    assert retro.logical_steps == (
        "improvement.retro",
        "improvement.retro-eval-analysis",
        "improvement.retro-issue-analysis",
        "improvement.retro-workflow-analysis",
    )
    assert improvement.logical_steps == (
        "improvement.improvement-review",
        "improvement.evaluate",
        "improvement.apply",
    )
    assert retro.status == "completed"
    assert improvement.status == "completed"


def test_improvement_evaluate_export_and_rollback_are_independent(installed_sources, tmp_path: Path):
    evaluate = _run(installed_sources, tmp_path, "improvement-evaluate")
    export = _run(installed_sources, tmp_path, "improvement-export")
    rollback = _run(installed_sources, tmp_path, "improvement-rollback")
    review = _run(installed_sources, tmp_path, "improvement-review")
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
