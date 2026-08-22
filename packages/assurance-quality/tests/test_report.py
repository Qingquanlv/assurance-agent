from __future__ import annotations

import pytest
from graph_engine.canonical import JSONValue
from graph_engine.plugin_api import CandidateWriteSet, ValidationResult
from tests.phase4.conformance import execute_task

from assurance_quality.operations.report import DashboardHandler, GenerateReportHandler
from assurance_quality.validators.report import ReportValidator
from quality_fixtures import (  # pyright: ignore[reportMissingImports]
    BATCH_ID,
    CHANGE_ID,
    HEX_A,
    HEX_B,
    as_object,
    validation_context,
    write_set,
)

SOURCE_PATHS = {
    "case": "cases/source-digest",
    "plan": "plans/source-digest",
    "mapping": "codegen/source-digest",
    "execution": "execution/source-digest",
    "healing": "healing/source-digest",
    "trace": "inspect/trace-projection.json",
    "coverage": "inspect/coverage-gaps.json",
    "issue": "issues/snapshot.json",
    "metrics": "inspect/metrics.json",
}


def candidate_report(*, missing: str | None = None) -> CandidateWriteSet:
    paths = ["report/quality-report.json"]
    paths.extend(path for key, path in SOURCE_PATHS.items() if key != missing)
    return write_set(*paths)


def _gate(*, failed: int = 1) -> JSONValue:
    status = "FAIL" if failed else "PASS"
    return {
        "schema_version": "2.0",
        "change_id": CHANGE_ID,
        "batch_id": BATCH_ID,
        "final_status": status,
        "dimensions": {
            "functional": {
                "status": status,
                "api": {"total": 1, "passed": 0 if failed else 1, "failed": failed},
                "e2e": {"total": 0, "passed": 0, "failed": 0},
            },
            "coverage": {
                "status": "SKIPPED",
                "available": False,
                "line_coverage": 0.0,
                "branch_coverage": 0.0,
                "threshold": {"line": 80.0, "branch": 0.0},
                "evidence": {"kind": "error", "error_code": "evidence_projection_missing"},
            },
        },
    }


def _analysis(*, category: str = "business_logic_failure") -> JSONValue:
    return {
        "schema_version": "1.0",
        "change_id": CHANGE_ID,
        "source_manifest": "execution/execution-manifest.json",
        "inspection_status": "completed",
        "batch_id": BATCH_ID,
        "source_batch_id": BATCH_ID,
        "final_status": "FAIL",
        "inspect_mode": "primary",
        "classification_performed": True,
        "status": "analyzed",
        "failures": [
            {
                "case_id": "TC_A",
                "target": "api",
                "category": category,
                "fix_proposal_eligible": False,
                "severity": "high",
                "evidence": {
                    "result_file": "execution/api-result.json",
                    "test_file": "tests/generated.py",
                    "trace": "",
                    "screenshot": "",
                    "video": "",
                    "raw_log": "",
                    "log_excerpt": "boom",
                },
                "diagnosis": "product defect",
                "recommended_action": "fix product",
            }
        ],
        "hard_fails": [],
        "needs_review": [],
        "known_product_issues": [],
    }


def report_input(**overrides: JSONValue) -> JSONValue:
    payload: dict[str, JSONValue] = {
        "change_id": CHANGE_ID,
        "batch_id": BATCH_ID,
        "quality_gate": _gate(),
        "analysis": _analysis(),
        "metrics": {"schema_version": "1.0", "metrics": {}},
        "scope": {"cases": 1, "requirements": ["REQ-1"]},
        "started_at": None,
        "duration": None,
        "case_digest": HEX_A,
        "plan_digest": HEX_B,
        "mapping_digest": HEX_A,
        "execution_digest": HEX_A,
        "healing_digest": HEX_B,
        "trace_digest": HEX_A,
        "coverage_digest": HEX_B,
        "issue_digest": HEX_B,
        "metrics_digest": HEX_A,
    }
    payload.update(overrides)
    return payload


def test_report_validator_requires_exact_quality_inputs() -> None:
    result = ReportValidator().validate(candidate_report(missing="metrics"), validation_context())
    assert result == ValidationResult(
        accepted=False,
        reason="quality report is missing the authenticated metrics projection",
    )


@pytest.mark.asyncio
async def test_generate_report_uses_gate_status_not_issue_risk() -> None:
    outcome = await execute_task(GenerateReportHandler(), report_input())
    assert outcome.status == "succeeded"
    payload = as_object(outcome.output)
    assert payload["schema_version"] == "1.1"
    assert payload["final_status"] == "FAIL"
    assert payload["quality_score"] == 0
    assert payload["recommendation"].startswith("Do not release")
    assert "session" not in str(payload).lower()
    assert "secret" not in str(payload).lower()


@pytest.mark.asyncio
async def test_generate_report_buckets_product_and_test_defects() -> None:
    outcome = await execute_task(
        GenerateReportHandler(),
        report_input(analysis=_analysis(category="locator_failure")),
    )
    assert outcome.status == "succeeded"
    defects = as_object(outcome.output)["defects"]
    assert defects["test"][0]["category"] == "locator_failure"
    assert defects["product"] == []


@pytest.mark.asyncio
async def test_dashboard_projects_report_without_transcript() -> None:
    report = await execute_task(GenerateReportHandler(), report_input())
    assert report.status == "succeeded"
    dashboard_input: JSONValue = {
        "change_id": CHANGE_ID,
        "batch_id": BATCH_ID,
        "report": report.output,
        "quality_gate": _gate(),
        "metrics_digest": HEX_A,
        "report_digest": HEX_B,
    }
    outcome = await execute_task(DashboardHandler(), dashboard_input)
    assert outcome.status == "succeeded"
    payload = as_object(outcome.output)
    assert payload["change_id"] == CHANGE_ID
    assert payload["final_status"] == "FAIL"
    assert payload["quality_score"] == 0
    assert "transcript" not in payload
    encoded = str(payload).lower()
    assert "opencode" not in encoded
    assert "secret" not in encoded
