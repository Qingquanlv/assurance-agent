from __future__ import annotations

import json
from pathlib import Path

import pytest

from assurance_agent.eval.scorers import get_scorer
from assurance_agent.eval.types import DatasetSample
from tests.unit.eval.attempt_fixtures import (
    make_attempt,
    write_case,
    write_layer_result,
    write_manifest,
    write_review,
    write_state,
)


def _sample(suite: str, sid: str = "S-1") -> DatasetSample:
    return DatasetSample(id=sid, suite=suite, input={"change_id": "eval-sample-001"}, expected={})


def test_unknown_suite_raises() -> None:
    with pytest.raises(KeyError):
        get_scorer("no-such-suite")


def test_case_generation_prf1_from_judge_result(tmp_path: Path) -> None:
    """Regression (user-reported): judge-result.json drives requirement P/R/F1."""
    attempt = make_attempt(tmp_path)
    (attempt / "judge-result.json").write_text(
        json.dumps(
            {
                "label": "covered",
                "reason": "all atoms covered",
                "evidence_refs": ["CG-001"],
                "confidence": 0.95,
                "needs_human_review": False,
            }
        ),
        encoding="utf-8",
    )
    sample = DatasetSample(
        id="CG-001", suite="case-generation", input={}, expected={"human_label": "covered"}
    )
    m = get_scorer("case-generation")(sample, attempt).metrics
    assert m["requirement_precision"] == 1.0
    assert m["requirement_recall"] == 1.0
    assert m["requirement_f1"] == 1.0


def test_workflow_case_all_green(tmp_path: Path) -> None:
    attempt = make_attempt(tmp_path)
    write_case(attempt, "users", valid=True)
    write_review(attempt, "pass")
    write_state(attempt, "done")
    score = get_scorer("workflow-case")(_sample("workflow-case"), attempt)
    m = score.metrics
    assert m["evidence_integrity"] == 1
    assert m["schema_valid_rate"] == 1.0  # 1/1 case ok × review present(1)
    assert m["case_review_gate_pass_rate"] == 1.0
    assert m["layer_scan_valid_rate"] == 1.0
    assert m["secret_leak_count"] == 0
    assert m["forbidden_write_executed_count"] == 0


def test_workflow_case_review_reject_and_bad_schema(tmp_path: Path) -> None:
    attempt = make_attempt(tmp_path)
    write_case(attempt, "users", valid=True)
    write_case(attempt, "roles", valid=False)  # 1 good / 1 bad → 0.5
    write_review(attempt, "reject")
    write_state(attempt, "done")
    m = get_scorer("workflow-case")(_sample("workflow-case"), attempt).metrics
    # schema_valid_rate = case_rate(0.5) * review_present(1) = 0.5
    assert m["schema_valid_rate"] == pytest.approx(0.5)
    assert m["case_review_gate_pass_rate"] == 0.0


def test_workflow_case_secret_leak_counted(tmp_path: Path) -> None:
    attempt = make_attempt(tmp_path, stdout="Authorization: Bearer sk-abc123DEF456ghi789JKL\n")
    write_review(attempt, "pass")
    m = get_scorer("workflow-case")(_sample("workflow-case"), attempt).metrics
    assert m["secret_leak_count"] >= 1


def test_workflow_run_e3_layer_rates(tmp_path: Path) -> None:
    attempt = make_attempt(tmp_path)
    write_manifest(attempt, "PASS", selected=["api"])
    write_layer_result(attempt, "api", passed=24, total=26, status="PASS")
    m = get_scorer("workflow-run")(_sample("workflow-run", "WR-001"), attempt).metrics
    assert m["evidence_integrity"] == 1
    assert m["execution_pass_rate"] == 1.0
    assert m["api_pass_rate"] == pytest.approx(24 / 26)
    assert m["test_executable_rate"] == 1.0  # api in-scope & executable


def test_workflow_run_test_executable_rate_skipped(tmp_path: Path) -> None:
    attempt = make_attempt(tmp_path)
    write_manifest(attempt, "FAIL", selected=["api", "e2e"])
    write_layer_result(attempt, "api", passed=1, total=1, status="PASS")
    write_layer_result(attempt, "e2e", passed=0, total=0, status="SKIPPED")
    m = get_scorer("workflow-run")(_sample("workflow-run", "WR-005"), attempt).metrics
    # in-scope=2 (api,e2e); executable=1 (api) → 0.5
    assert m["test_executable_rate"] == pytest.approx(0.5)
    assert m["execution_pass_rate"] == 0.0


def test_codegen_scorer_py_syntax_and_summary(tmp_path: Path) -> None:
    attempt = make_attempt(tmp_path)
    tests_api = attempt / "raw-output" / "tests" / "api"
    tests_api.mkdir(parents=True)
    (tests_api / "test_ok.py").write_text("def test_x():\n    assert True\n", encoding="utf-8")
    (tests_api / "test_bad.py").write_text("def test_y(:\n", encoding="utf-8")  # syntax error
    codegen = attempt / "raw-output" / "codegen"
    codegen.mkdir(parents=True)
    (codegen / "api-codegen-summary.md").write_text("# summary\n", encoding="utf-8")
    m = get_scorer("workflow-api-codegen")(_sample("workflow-api-codegen", "WAC-001"), attempt).metrics
    assert m["schema_valid_rate"] == pytest.approx(0.5)  # 1 of 2 py compiles
    assert m["codegen_summary_present_rate"] == 1.0
    # Task 18 dark-ship: live scorer must not expose the three future hard metrics.
    assert "current_assurance_chain_rate" not in m
    assert "current_codegen_attempt_rate" not in m
    assert "selected_test_write_rate" not in m


def test_workflow_full_observe_only(tmp_path: Path) -> None:
    attempt = make_attempt(tmp_path)
    write_manifest(attempt, "PASS_WITH_WARNINGS", selected=["api"])
    m = get_scorer("workflow-full")(_sample("workflow-full", "WF-001"), attempt).metrics
    assert m["full_run_completed_rate"] == 1.0
    assert m["end_to_end_pass_rate"] == 1.0
