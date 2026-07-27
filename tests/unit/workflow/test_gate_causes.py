from __future__ import annotations

from pathlib import Path

import pytest

from assurance_agent.workflow.orchestration.gates import GateEvaluationContext, check_gate_in_view
from assurance_agent.workflow.orchestration.schema import normalize_gates


def _gates(stop_when: str | None = None):
    stop_expr = stop_when or (
        "execution.final_status == 'FAIL' "
        "or healing.status in ['applied','exhausted','failed'] "
        "or failure_analysis.source_batch_id != execution.batch_id"
    )
    return normalize_gates(
        {
            "archive-gate": {
                "reads": [
                    {"path": "execution.json", "as": "execution"},
                    {"path": "healing.json", "as": "healing"},
                    {"path": "failure-analysis.json", "as": "failure_analysis"},
                ],
                "stop_when": stop_expr,
                "pass_when": "execution.final_status == 'PASS'",
                "causes": {
                    "archive.execution_failed": "execution.final_status == 'FAIL'",
                    "archive.healing_unsettled": ("healing.status in ['applied','exhausted','failed']"),
                    "archive.batch_mismatch": ("failure_analysis.source_batch_id != execution.batch_id"),
                },
            }
        }
    )


def _context(tmp_path: Path, *, execution: dict, healing: dict, failure_analysis: dict):
    change_dir = tmp_path / "qa" / "changes" / "CH-1"
    change_dir.mkdir(parents=True)
    return GateEvaluationContext(
        project_root=tmp_path,
        repo_root=tmp_path,
        change_dir=change_dir,
        change_id="CH-1",
        params={},
        state_values={},
        node_results={},
        artifact_overrides={
            "execution.json": execution,
            "healing.json": healing,
            "failure-analysis.json": failure_analysis,
        },
    )


@pytest.mark.parametrize(
    ("execution", "healing", "failure_analysis", "expected"),
    [
        (
            {"final_status": "FAIL", "batch_id": "b1"},
            {"status": "resolved"},
            {"source_batch_id": "b1"},
            "archive.execution_failed",
        ),
        (
            {"final_status": "PASS", "batch_id": "b1"},
            {"status": "applied"},
            {"source_batch_id": "b1"},
            "archive.healing_unsettled",
        ),
        (
            {"final_status": "PASS", "batch_id": "b2"},
            {"status": "resolved"},
            {"source_batch_id": "b1"},
            "archive.batch_mismatch",
        ),
    ],
)
def test_stop_report_records_first_structured_cause(
    tmp_path: Path,
    execution: dict,
    healing: dict,
    failure_analysis: dict,
    expected: str,
) -> None:
    report = check_gate_in_view(
        _gates(),
        "archive-gate",
        _context(
            tmp_path,
            execution=execution,
            healing=healing,
            failure_analysis=failure_analysis,
        ),
    )

    assert report.verdict.value == "stop"
    assert report.details == {"cause": expected}


def test_stop_with_no_matching_cause_is_visible_as_unknown(tmp_path: Path) -> None:
    report = check_gate_in_view(
        _gates("execution.final_status == 'PASS_WITH_WARNINGS'"),
        "archive-gate",
        _context(
            tmp_path,
            execution={"final_status": "PASS_WITH_WARNINGS", "batch_id": "b1"},
            healing={"status": "resolved"},
            failure_analysis={"source_batch_id": "b1"},
        ),
    )

    assert report.verdict.value == "stop"
    assert report.details == {"cause": "archive-gate.unknown"}


def test_pass_report_has_no_cause(tmp_path: Path) -> None:
    report = check_gate_in_view(
        _gates(),
        "archive-gate",
        _context(
            tmp_path,
            execution={"final_status": "PASS", "batch_id": "b1"},
            healing={"status": "resolved"},
            failure_analysis={"source_batch_id": "b1"},
        ),
    )

    assert report.verdict.value == "pass"
    assert report.details is None
