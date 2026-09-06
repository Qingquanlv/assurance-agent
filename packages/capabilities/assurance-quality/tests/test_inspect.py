from __future__ import annotations

from typing import Any, cast

import pytest
from graph_engine.canonical import JSONValue
from tests.phase4.conformance import execute_task

from assurance_execution.contracts.evidence import ExecutionEvidenceV1
from assurance_healing.contracts.status import HealingStatusV1
from assurance_quality.operations.inspect import InspectHandler, classify_failure
from quality_fixtures import (  # pyright: ignore[reportMissingImports]
    BATCH_ID,
    CASE_ID,
    CHANGE_ID,
    HEX_A,
    HEX_B,
    LEAF,
    as_object,
    digest_of,
)


def _dumped(model: type[Any], data: dict[str, object]) -> dict[str, object]:
    return cast(dict[str, object], model.model_validate(data).model_dump(mode="json", exclude_none=True))


def execution_document(
    *,
    message: str,
    target: str = "api",
    status: str = "failed",
) -> dict[str, object]:
    selected = {"api": False, "e2e": False, "fuzz": False, "performance": False}
    layer = target if target in selected else "api"
    selected[layer] = True
    return _dumped(
        ExecutionEvidenceV1,
        {
            "schema_version": "1",
            "change_id": CHANGE_ID,
            "plan_digest": HEX_A,
            "plan_ref": {
                "path": f"qa/changes/{CHANGE_ID}/plan/{HEX_A}/resolved-assurance-plan.json",
                "digest": HEX_A,
            },
            "batch_id": BATCH_ID,
            "selected_targets": selected,
            "mapping": {
                "schema_version": "1",
                "selected": ["tests/generated.py"],
                "mappings": [
                    {
                        "test": "tests/generated.py",
                        "case_id": CASE_ID,
                        "capability": LEAF,
                        "layer": layer,
                    }
                ],
            },
            "mapping_digest": HEX_A,
            "baseline_tree_id": HEX_A,
            "runner_profile_digest": HEX_A,
            "receipt_digest": HEX_A,
            "receipt": {
                "commands": [
                    {
                        "family": layer,
                        "command": ["pytest"],
                        "exit_code": 0 if status == "passed" else 1,
                        "collected": 1,
                        "passed": 1 if status == "passed" else 0,
                        "failed": 0 if status == "passed" else 1,
                        "skipped": 0,
                    }
                ]
            },
            "results": [
                {
                    "test": "tests/generated.py",
                    "status": status,
                    "duration_ms": 1,
                    "message": message,
                    "case_id": CASE_ID,
                }
            ],
        },
    )


def missing_asset_execution() -> dict[str, object]:
    return _dumped(
        ExecutionEvidenceV1,
        {
            "schema_version": "1",
            "change_id": CHANGE_ID,
            "plan_digest": HEX_A,
            "plan_ref": {
                "path": f"qa/changes/{CHANGE_ID}/plan/{HEX_A}/resolved-assurance-plan.json",
                "digest": HEX_A,
            },
            "batch_id": BATCH_ID,
            "selected_targets": {"api": True, "e2e": False, "fuzz": False, "performance": False},
            "mapping": {
                "schema_version": "1",
                "selected": ["tests/generated.py"],
                "mappings": [
                    {
                        "test": "tests/generated.py",
                        "case_id": CASE_ID,
                        "capability": LEAF,
                        "layer": "api",
                    }
                ],
            },
            "mapping_digest": HEX_A,
            "baseline_tree_id": HEX_A,
            "runner_profile_digest": HEX_A,
            "receipt_digest": HEX_A,
            "receipt": {
                "commands": [
                    {
                        "family": "api",
                        "command": ["pytest", "tests/generated.py"],
                        "exit_code": 0,
                        "collected": 1,
                        "passed": 0,
                        "failed": 0,
                        "skipped": 1,
                    }
                ]
            },
            "results": [
                {
                    "test": "tests/generated.py",
                    "status": "skipped",
                    "duration_ms": 0,
                    "message": "execution asset missing",
                    "case_id": CASE_ID,
                }
            ],
        },
    )


def healing_document() -> dict[str, object]:
    return _dumped(
        HealingStatusV1,
        {"schema_version": "1", "change_id": CHANGE_ID, "status": "not_needed", "attempts_used": 0},
    )


def sufficient_report() -> dict[str, object]:
    return {
        "schema_version": "2.0",
        "source_projection_digest": HEX_A,
        "source_policy_digest": HEX_B,
        "semantics": "evidence_sufficiency/v2",
        "require_current_batch": True,
        "as_of": "2026-08-22T00:00:00Z",
        "recency_hours": 24,
        "verdicts": [],
    }


def inspect_input(
    *, message: str, target: str = "api", status: str = "failed", **overrides: object
) -> JSONValue:
    execution = execution_document(message=message, target=target, status=status)
    healing = healing_document()
    trace = {"kind": "trace"}
    coverage = {"kind": "coverage"}
    metrics = {"kind": "metrics"}
    payload: dict[str, object] = {
        "change_id": CHANGE_ID,
        "batch_id": BATCH_ID,
        "execution": execution,
        "healing": healing,
        "trace": trace,
        "coverage": coverage,
        "metrics": metrics,
        "execution_digest": digest_of(execution),
        "healing_digest": digest_of(healing),
        "trace_digest": digest_of(trace),
        "coverage_digest": digest_of(coverage),
        "metrics_digest": digest_of(metrics),
        "result_paths": {target: f"execution/{target}-result.json"},
    }
    payload.update(overrides)
    return cast(JSONValue, payload)


def test_classify_api_locator_is_unknown_not_auto_fixable() -> None:
    result = classify_failure(message="locator not found: .card", target="api")
    assert result.category == "unknown"
    assert result.fix_proposal_eligible is False


def test_classify_environment_timeout_beats_wait_strategy() -> None:
    result = classify_failure(message="timeout connecting to host", target="api")
    assert result.category == "environment_failure"
    assert result.fix_proposal_eligible is False
    assert result.severity == "critical"


def test_classify_api_404_is_business_logic() -> None:
    result = classify_failure(message="404 not found", target="api")
    assert result.category == "business_logic_failure"
    assert result.fix_proposal_eligible is False


def test_classify_fuzz_fallback_is_stateful() -> None:
    result = classify_failure(message="unknown boom", target="fuzz")
    assert result.category == "fuzz_stateful_failure"
    assert result.needs_review is True


def test_classify_fixture_missing_is_auto_fixable_test_data() -> None:
    result = classify_failure(message="fixture missing", target="api")
    assert result.category == "test_data_failure"
    assert result.fix_proposal_eligible is True
    assert result.needs_review is False


@pytest.mark.asyncio
async def test_inspect_classifies_locator_failure() -> None:
    outcome = await execute_task(
        InspectHandler(), inspect_input(message="locator not found: .card", target="e2e")
    )
    assert outcome.status == "succeeded"
    payload = as_object(outcome.output)
    analysis = payload["analysis"]
    assert analysis["failures"][0]["category"] == "locator_failure"
    assert analysis["failures"][0]["fix_proposal_eligible"] is True
    assert analysis["final_status"] == "FAIL"
    assert payload["quality_gate"]["final_status"] == "FAIL"
    assert "transcript" not in payload


@pytest.mark.asyncio
async def test_inspect_api_locator_is_unknown() -> None:
    outcome = await execute_task(
        InspectHandler(), inspect_input(message="locator not found: .card", target="api")
    )
    assert outcome.status == "succeeded"
    failure = as_object(outcome.output)["analysis"]["failures"][0]
    assert failure["category"] == "unknown"
    assert failure["fix_proposal_eligible"] is False


@pytest.mark.asyncio
async def test_inspect_environment_timeout_is_environment_failure() -> None:
    outcome = await execute_task(
        InspectHandler(),
        inspect_input(message="timeout connecting to host", target="api"),
    )
    assert outcome.status == "succeeded"
    failure = as_object(outcome.output)["analysis"]["failures"][0]
    assert failure["category"] == "environment_failure"
    assert failure["fix_proposal_eligible"] is False


@pytest.mark.asyncio
async def test_inspect_classifies_assertion_as_not_auto_fixable() -> None:
    outcome = await execute_task(InspectHandler(), inspect_input(message="AssertionError: expected 200"))
    assert outcome.status == "succeeded"
    failure = as_object(outcome.output)["analysis"]["failures"][0]
    assert failure["category"] == "assertion_failure"
    assert failure["fix_proposal_eligible"] is False


@pytest.mark.asyncio
async def test_inspect_integrity_issue_is_critical_manifest_failure() -> None:
    payload = inspect_input(
        message="",
        status="passed",
        execution=missing_asset_execution(),
        execution_digest=digest_of(missing_asset_execution()),
        result_paths={},
        integrity_issues=[
            {
                "target": "api",
                "path": "execution/api-result.json",
                "reason": "selected target has no result file",
            }
        ],
    )
    outcome = await execute_task(InspectHandler(), payload)
    assert outcome.status == "succeeded"
    analysis = as_object(outcome.output)["analysis"]
    assert analysis["status"] == "failed"
    assert analysis["failures"][0]["category"] == "manifest_asset_missing"
    assert analysis["final_status"] == "FAIL"


@pytest.mark.asyncio
async def test_inspect_failed_performance_fails_gate() -> None:
    outcome = await execute_task(
        InspectHandler(),
        inspect_input(
            message="",
            status="passed",
            sufficiency=sufficient_report(),
            performance={
                "available": True,
                "status": "FAIL",
                "scenarios": [
                    {
                        "capability": LEAF,
                        "endpoint": "POST /menus",
                        "measured_p95_ms": 900.0,
                        "threshold_p95_ms": 200.0,
                        "measured_error_rate": 0.0,
                        "threshold_error_rate_max": 0.01,
                        "verdict": "FAIL",
                    }
                ],
            },
        ),
    )
    assert outcome.status == "succeeded"
    gate = as_object(outcome.output)["quality_gate"]
    assert gate["dimensions"]["non_functional"]["status"] == "FAIL"
    assert gate["final_status"] == "FAIL"


@pytest.mark.asyncio
async def test_inspect_rejects_execution_digest_that_does_not_match_document() -> None:
    outcome = await execute_task(
        InspectHandler(),
        inspect_input(message="AssertionError: expected 200", execution_digest=HEX_B),
    )
    assert outcome.status == "failed"
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_input"


@pytest.mark.asyncio
async def test_inspect_unmapped_tests_warn_and_missing_sufficiency_fails_coverage() -> None:
    warned = await execute_task(
        InspectHandler(),
        inspect_input(
            message="",
            status="passed",
            sufficiency=sufficient_report(),
            unmapped_tests=[
                {
                    "case_id": "UNMAPPED-orphan",
                    "test_name": "test_orphan",
                    "status": "passed",
                    "target": "api",
                    "message": "",
                    "file": "tests/orphan.py",
                }
            ],
        ),
    )
    assert warned.status == "succeeded"
    warned_gate = as_object(warned.output)["quality_gate"]
    assert warned_gate["final_status"] == "PASS_WITH_WARNINGS"
    assert warned_gate["dimensions"]["functional"]["unmapped_tests"] == 1
    assert any("TRACEABILITY-BROKEN" in item for item in (warned_gate.get("warnings") or []))

    missing = await execute_task(
        InspectHandler(),
        inspect_input(
            message="",
            status="passed",
            coverage_metrics={
                "available": True,
                "line_coverage": 10.0,
                "branch_coverage": 10.0,
                "threshold_line": 80.0,
                "threshold_branch": 0.0,
                "uncovered_critical_files": [],
            },
        ),
    )
    assert missing.status == "succeeded"
    missing_gate = as_object(missing.output)["quality_gate"]
    assert missing_gate["dimensions"]["coverage"]["status"] == "FAIL"
    assert missing_gate["dimensions"]["coverage"]["evidence"] == {
        "kind": "error",
        "error_code": "evidence_projection_missing",
    }
    assert missing_gate["final_status"] == "FAIL"
