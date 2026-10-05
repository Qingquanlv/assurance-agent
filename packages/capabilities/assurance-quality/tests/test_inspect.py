from __future__ import annotations

from typing import Any, cast

from graph_engine.canonical import JSONValue

from assurance_execution.contracts.evidence import ExecutionEvidenceV1
from assurance_healing.contracts.status import HealingStatusV1
from assurance_quality.operations.inspect import classify_failure
from quality_fixtures import (  # pyright: ignore[reportMissingImports]
    BATCH_ID,
    CASE_ID,
    CHANGE_ID,
    HEX_A,
    HEX_B,
    LEAF,
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
                "path": f"qa/results/plan/{HEX_A}/resolved-assurance-plan.json",
                "digest": HEX_A,
            },
            "batch_id": BATCH_ID,
            "selected_targets": selected,
            "family_outcomes": [{"family": layer, "state": "executed"}],
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
                "path": f"qa/results/plan/{HEX_A}/resolved-assurance-plan.json",
                "digest": HEX_A,
            },
            "batch_id": BATCH_ID,
            "selected_targets": {"api": True, "e2e": False, "fuzz": False, "performance": False},
            "family_outcomes": [{"family": "api", "state": "executed"}],
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
