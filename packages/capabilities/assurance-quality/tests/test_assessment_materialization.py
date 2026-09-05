from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from graph_engine.canonical import JSONValue, canonical_json_bytes

from assurance_quality.contracts.assessment import AssessmentInputsV1
from assurance_quality.contracts.metrics import MetricsDocument
from assurance_quality.contracts.sufficiency import TraceSufficiencyFacts
from assurance_quality.contracts.trace import TraceProjectionV2
from assurance_quality.operations.assessment import MaterializeAssessmentHandler
from tests.acg_plan_fixture import install_plan
from tests.phase4.conformance import execute_task

CHANGE_ID = "CH-ASSESS-001"
BATCH_ID = "20260905T010203Z"
EXECUTED_AT = datetime(2026, 9, 5, 1, 2, 3, tzinfo=UTC)
CASE_PATH = f"qa/changes/{CHANGE_ID}/cases/items/case.yaml"
MAPPING_PATH = f"qa/changes/{CHANGE_ID}/codegen/closed-mapping.json"
EVIDENCE_PATH = f"qa/changes/{CHANGE_ID}/execution/execute-result.json"
TEST_SELECTOR = "tests/api/test_items.py::test_create_item"
CAPABILITY = "entities.item.constraints.name"


def _case(case_id: str, capability: str) -> dict[str, object]:
    return {
        "case_id": case_id,
        "title": f"{case_id} behavior",
        "status": "active",
        "priority": "P1",
        "severity": "major",
        "type": "API",
        "module": "items",
        "requirement_id": "REQ-1",
        "feature_name": "item-management",
        "test_condition_id": f"COND-{case_id}",
        "design_technique": "use_case",
        "objective": "verify item behavior",
        "summary": "exercise and assert item behavior",
        "preconditions": [],
        "test_data": [],
        "steps": ["perform the operation"],
        "assertions": ["the operation succeeds"],
        "postconditions": [],
        "edge_cases": [],
        "related_cases": [],
        "risk": {
            "level": "high",
            "likelihood": 3,
            "impact": 4,
            "rationale": "important path",
        },
        "automation": {
            "required": True,
            "framework": "pytest",
            "status": "automated",
        },
        "regression": {
            "candidate": True,
            "tier": "smoke",
            "rationale": "protect the path",
            "selection_reason": ["critical_user_journey"],
            "maintenance_rule": "keep_until_feature_deprecated",
        },
        "trace": {capability: {"covered": True}},
    }


def _write_bytes(root: Path, relative: str, data: bytes) -> dict[str, str]:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return {"path": relative, "digest": hashlib.sha256(data).hexdigest()}


def _write_text(root: Path, relative: str, text: str) -> dict[str, str]:
    return _write_bytes(root, relative, text.encode())


def _write_json(root: Path, relative: str, value: object) -> dict[str, str]:
    return _write_bytes(root, relative, canonical_json_bytes(cast(JSONValue, value)))


def _workspace_input(root: Path) -> dict[str, Any]:
    plan_document, plan_ref = install_plan(
        root,
        CHANGE_ID,
        capability_leafs=(CAPABILITY, "entities.item.constraints.description"),
        minimum_required_coverage={
            "api": [CAPABILITY, "entities.item.constraints.description"],
        },
    )
    preparation = _write_text(root, f"qa/changes/{CHANGE_ID}/requirement.md", "# Requirement\n")
    review = _write_json(
        root,
        f"qa/changes/{CHANGE_ID}/review/case-review.json",
        {"decision": "approved"},
    )
    case_ref = _write_text(
        root,
        CASE_PATH,
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "added": [
                    _case("TC_ITEM_001", CAPABILITY),
                    _case("TC_ITEM_002", "entities.item.constraints.description"),
                ],
                "modified": [],
                "removed": [],
            },
            sort_keys=True,
        ),
    )
    matrix_ref = _write_json(
        root,
        f"qa/changes/{CHANGE_ID}/trace/minimum-coverage-matrix.json",
        [
            {
                "mrc_id": "MRC-API-001",
                "key": CAPABILITY,
                "category": "api",
                "required": True,
                "layer": "api",
                "covered_by_cases": ["TC_ITEM_001"],
                "status": "covered",
            },
            {
                "mrc_id": "MRC-API-002",
                "key": "entities.item.constraints.description",
                "category": "api",
                "required": True,
                "layer": "api",
                "covered_by_cases": ["TC_ITEM_002"],
                "status": "covered",
            },
        ],
    )
    source = _write_text(
        root,
        f"qa/changes/{CHANGE_ID}/generated/api/files/tests/api/test_items.py",
        "def test_create_item():\n    assert True\n",
    )
    plan = _write_text(root, f"qa/changes/{CHANGE_ID}/plans/api-plan.md", "# Plan\n")
    mapping = {
        "schema_version": "1",
        "selected": [TEST_SELECTOR],
        "mappings": [
            {
                "test": TEST_SELECTOR,
                "case_id": "TC_ITEM_001",
                "capability": CAPABILITY,
                "layer": "api",
            }
        ],
    }
    mapping_ref = _write_json(root, MAPPING_PATH, mapping)
    evidence = {
        "schema_version": "1",
        "status": "passed",
        "change_id": CHANGE_ID,
        "batch_id": BATCH_ID,
        "executed_at": EXECUTED_AT.isoformat(),
        "plan_digest": plan_document.plan_digest,
        "plan_ref": plan_ref,
        "selected_targets": {
            "api": True,
            "e2e": False,
            "fuzz": False,
            "performance": False,
        },
        "mapping": mapping,
        "mapping_digest": mapping_ref["digest"],
        "baseline_tree_id": "b" * 64,
        "runner_profile_digest": "c" * 64,
        "receipt_digest": "d" * 64,
        "receipt": {
            "commands": [
                {
                    "family": "api",
                    **{
                        "command": ["pytest", TEST_SELECTOR],
                        "exit_code": 0,
                        "collected": 1,
                        "passed": 1,
                        "failed": 0,
                        "skipped": 0,
                    },
                }
            ]
        },
        "results": [
            {
                "test": TEST_SELECTOR,
                "status": "passed",
                "duration_ms": 5,
                "case_id": "TC_ITEM_001",
            }
        ],
    }
    evidence_ref = _write_json(root, EVIDENCE_PATH, evidence)
    reviewed = {
        "change_id": CHANGE_ID,
        "coverage_epoch": 3,
        "plan_digest": plan_document.plan_digest,
        "plan_ref": plan_ref,
        "preparation_refs": sorted(
            [preparation, matrix_ref, plan_ref],
            key=lambda item: (item["path"], item["digest"]),
        ),
        "case_refs": [case_ref],
        "review_ref": review,
    }
    generation = {
        "change_id": CHANGE_ID,
        "coverage_epoch": 3,
        "plan_digest": plan_document.plan_digest,
        "plan_ref": plan_ref,
        "reviewed_case": reviewed,
        "mapping_ref": mapping_ref,
        "source_refs": [source],
        "plan_refs": [plan],
    }
    return {
        "plan_digest": plan_document.plan_digest,
        "plan_ref": plan_ref,
        "reviewed_case": reviewed,
        "generation": generation,
        "execution": {
            "change_id": CHANGE_ID,
            "coverage_epoch": 3,
            "repair_round": 0,
            "batch_id": BATCH_ID,
            "executed_at": EXECUTED_AT.isoformat(),
            "plan_digest": plan_document.plan_digest,
            "plan_ref": plan_ref,
            "final_status": "PASS",
            "evidence_ref": evidence_ref,
            "mapping_ref": mapping_ref,
            "source_refs": [source],
            "receipt": {"receipt_id": "execute", "receipt_digest": "e" * 64},
        },
        "policy_resource_id": "assurance.product.configuration.product-policy",
        "policy_sha256": plan_document.policy_digest,
        "execution_at": EXECUTED_AT.isoformat(),
    }


@pytest.mark.asyncio
async def test_materializes_authenticated_case_mapping_execution_and_policy(tmp_path: Path) -> None:
    request = _workspace_input(tmp_path)

    result = await execute_task(MaterializeAssessmentHandler(), request, tmp_path)

    assert result.status == "succeeded", result.failure
    output = AssessmentInputsV1.model_validate(result.output)
    assert output.change_id == CHANGE_ID
    assert output.coverage_epoch == 3
    assert output.batch_id == BATCH_ID
    assert output.scope.required_case_ids == ("TC_ITEM_001", "TC_ITEM_002")
    assert output.scope.selected_families == ("api",)
    assert output.scope.applicable_goals == ("constraint_coverage",)
    expected = {
        output.trace_ref.path,
        output.gaps_ref.path,
        output.metrics_ref.path,
        output.sufficiency_ref.path,
    }
    assert expected <= result.workspace_bytes.keys()

    trace = TraceProjectionV2.model_validate(
        json.loads(result.workspace_bytes[output.trace_ref.path]),
        context={
            "capability_leafs": frozenset({CAPABILITY, "entities.item.constraints.description"}),
            "case_ids": frozenset({"TC_ITEM_001", "TC_ITEM_002"}),
            "test_ids": frozenset({TEST_SELECTOR}),
        },
    )
    assert [row.case_id for row in trace.rows] == ["TC_ITEM_001", "TC_ITEM_002"]
    assert trace.rows[0].freshest_pass is not None
    assert trace.rows[1].latest_execution is None

    sufficiency = TraceSufficiencyFacts.model_validate(
        json.loads(result.workspace_bytes[output.sufficiency_ref.path])
    )
    assert sufficiency.sufficient is False
    assert sufficiency.insufficient_cases[0].case_id == "TC_ITEM_002"
    assert sufficiency.insufficient_cases[0].reason_codes == (
        "not_in_current_batch",
        "uncovered",
        "never_run",
        "no_pass",
    )
    metrics = MetricsDocument.model_validate(json.loads(result.workspace_bytes[output.metrics_ref.path]))
    assert metrics.metrics["constraint_coverage"].value == 0.5
    assert metrics.computed_at == EXECUTED_AT


@pytest.mark.asyncio
async def test_rejects_batch_identity_mismatch(tmp_path: Path) -> None:
    request = _workspace_input(tmp_path)
    evidence = json.loads((tmp_path / EVIDENCE_PATH).read_bytes())
    evidence["batch_id"] = "OTHER"
    request["execution"]["evidence_ref"] = _write_json(tmp_path, EVIDENCE_PATH, evidence)

    result = await execute_task(MaterializeAssessmentHandler(), request, tmp_path)

    assert result.status == "failed"
    assert result.failure is not None
    assert result.failure.kind == "invalid_input"
    assert "identity" in result.failure.message


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["policy", "case", "mapping"])
async def test_rejects_locked_source_digest_drift(tmp_path: Path, source: str) -> None:
    request = _workspace_input(tmp_path)
    paths = {
        "policy": ".aa/policy.yaml",
        "case": CASE_PATH,
        "mapping": MAPPING_PATH,
    }
    with (tmp_path / paths[source]).open("ab") as stream:
        stream.write(b"\n# drift\n")

    result = await execute_task(MaterializeAssessmentHandler(), request, tmp_path)

    assert result.status == "failed"
    assert result.failure is not None
    assert result.failure.kind == "invalid_input"
    assert "digest changed" in result.failure.message


@pytest.mark.asyncio
async def test_rejects_missing_locked_mapping(tmp_path: Path) -> None:
    request = _workspace_input(tmp_path)
    (tmp_path / MAPPING_PATH).unlink()

    result = await execute_task(MaterializeAssessmentHandler(), request, tmp_path)

    assert result.status == "failed"
    assert result.failure is not None
    assert result.failure.kind == "invalid_input"
    assert "missing" in result.failure.message


@pytest.mark.asyncio
async def test_optional_healing_and_issue_evidence_are_digest_authenticated(tmp_path: Path) -> None:
    request = _workspace_input(tmp_path)
    request["healing_ref"] = _write_json(
        tmp_path,
        f"qa/changes/{CHANGE_ID}/healing/fix-proposal.json",
        {"status": "applied"},
    )
    request["issue_ref"] = _write_json(
        tmp_path,
        "issues/snapshot.json",
        {"problems": []},
    )
    with (tmp_path / "issues/snapshot.json").open("ab") as stream:
        stream.write(b"\n")

    result = await execute_task(MaterializeAssessmentHandler(), request, tmp_path)

    assert result.status == "failed"
    assert result.failure is not None
    assert result.failure.kind == "invalid_input"
    assert "digest changed" in result.failure.message
