from __future__ import annotations

import pytest
from graph_engine.canonical import JSONValue
from tests.phase4.conformance import execute_task

from assurance_quality.operations.inspect import InspectHandler
from quality_fixtures import (  # pyright: ignore[reportMissingImports]
    BATCH_ID,
    CHANGE_ID,
    HEX_A,
    HEX_B,
    as_object,
)


def inspect_input(*, message: str, target: str = "api") -> JSONValue:
    return {
        "change_id": CHANGE_ID,
        "batch_id": BATCH_ID,
        "execution_digest": HEX_A,
        "healing_digest": HEX_B,
        "trace_digest": HEX_A,
        "coverage_digest": HEX_B,
        "metrics_digest": HEX_A,
        "result_paths": {"api": "execution/api-result.json"},
        "cases": [
            {
                "case_id": "TC_A",
                "test_name": "test_tc_a",
                "status": "failed",
                "target": target,
                "message": message,
                "file": "tests/generated.py",
            }
        ],
        "coverage": {
            "available": False,
            "line_coverage": 0.0,
            "branch_coverage": 0.0,
            "threshold_line": 80.0,
            "threshold_branch": 0.0,
            "uncovered_critical_files": [],
        },
    }


@pytest.mark.asyncio
async def test_inspect_classifies_locator_failure() -> None:
    outcome = await execute_task(InspectHandler(), inspect_input(message="locator not found: .card"))
    assert outcome.status == "succeeded"
    payload = as_object(outcome.output)
    analysis = payload["analysis"]
    assert analysis["failures"][0]["category"] == "locator_failure"
    assert analysis["failures"][0]["fix_proposal_eligible"] is True
    assert analysis["final_status"] == "FAIL"
    assert payload["quality_gate"]["final_status"] == "FAIL"
    assert "transcript" not in payload


@pytest.mark.asyncio
async def test_inspect_classifies_assertion_as_not_auto_fixable() -> None:
    outcome = await execute_task(InspectHandler(), inspect_input(message="AssertionError: expected 200"))
    assert outcome.status == "succeeded"
    failure = as_object(outcome.output)["analysis"]["failures"][0]
    assert failure["category"] == "assertion_failure"
    assert failure["fix_proposal_eligible"] is False


@pytest.mark.asyncio
async def test_inspect_integrity_issue_is_critical_manifest_failure() -> None:
    payload: JSONValue = {
        "change_id": CHANGE_ID,
        "batch_id": BATCH_ID,
        "execution_digest": HEX_A,
        "healing_digest": HEX_B,
        "trace_digest": HEX_A,
        "coverage_digest": HEX_B,
        "metrics_digest": HEX_A,
        "result_paths": {},
        "cases": [],
        "integrity_issues": [
            {
                "target": "api",
                "path": "execution/api-result.json",
                "reason": "selected target has no result file",
            }
        ],
    }
    outcome = await execute_task(InspectHandler(), payload)
    assert outcome.status == "succeeded"
    analysis = as_object(outcome.output)["analysis"]
    assert analysis["status"] == "failed"
    assert analysis["failures"][0]["category"] == "manifest_asset_missing"
    assert analysis["final_status"] == "FAIL"
