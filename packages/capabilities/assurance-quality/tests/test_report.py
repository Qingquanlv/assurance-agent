from __future__ import annotations

import pytest
from graph_engine.canonical import JSONValue
from graph_engine.plugin_api import CandidateWriteSet
from pydantic import ValidationError

from assurance_quality.contracts.report import QualityReport
from quality_fixtures import (  # pyright: ignore[reportMissingImports]
    BATCH_ID,
    CHANGE_ID,
    HEX_A,
    HEX_B,
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
        "plan_digest": HEX_A,
        "plan_ref": {
            "path": f"qa/results/plan/{HEX_A}/resolved-assurance-plan.json",
            "digest": HEX_B,
        },
        "quality_gate": _gate(),
        "analysis": _analysis(),
        "metrics": {"schema_version": "1.0", "metrics": {}},
        "scope": {"cases": 1, "requirements": ["REQ-1"]},
        "started_at": None,
        "duration": None,
        "execution_digest": HEX_A,
        "healing_digest": HEX_B,
        "trace_digest": HEX_A,
        "coverage_digest": HEX_B,
        "metrics_digest": HEX_A,
    }
    payload.update(overrides)
    return payload


def report_v1_document() -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "change_id": CHANGE_ID,
        "batch_id": BATCH_ID,
        "final_status": "FAIL",
        "quality_score": 0,
        "score_breakdown": {"functional": 0, "coverage": 0, "fuzz": "N/A", "performance": "N/A"},
        "scope": {"cases": 1, "requirements": ["REQ-1"]},
        "functional": {
            "status": "FAIL",
            "api": {"total": 1, "passed": 0, "failed": 1},
            "e2e": {"total": 0, "passed": 0, "failed": 0},
        },
        "coverage": {
            "status": "SKIPPED",
            "available": False,
            "line_coverage": 0.0,
            "branch_coverage": 0.0,
            "threshold": {"line": 80.0, "branch": 0.0},
        },
        "defects": {"product": [], "test": [], "environment": []},
        "risk_level": "HIGH",
        "risk_rationale": "failures present",
        "recommendation": "Do not release",
    }


def test_quality_report_accepts_only_schema_1_1() -> None:
    with pytest.raises(ValidationError):
        QualityReport.model_validate(report_v1_document())
