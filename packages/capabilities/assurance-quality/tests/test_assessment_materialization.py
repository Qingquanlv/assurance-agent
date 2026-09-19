from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

import pytest
import yaml
from pydantic import ValidationError

from graph_engine.canonical import JSONValue, canonical_json_bytes
from graph_engine.plugin_api import ResourceClaimTemplate

from assurance_execution.contracts.evidence import ExecutionEvidenceV1
from assurance_intake.contracts.common import TestFamily
from assurance_quality.contracts.assessment import AssessmentInputsV1, MaterializeAssessmentInputV1
from assurance_quality.contracts.attempts import TASK_ATTEMPT_CONTRACTS
from assurance_quality.contracts.coverage import CoverageGapsDocument, classify_coverage_state
from assurance_quality.contracts.decisions import classify_inspection_disposition
from assurance_quality.contracts.metrics import MetricKey, MetricsDocument
from assurance_quality.contracts.issues import IssueEvidenceManifest, ObservationDocument
from assurance_quality.contracts.sufficiency import TraceSufficiencyFacts
from assurance_quality.contracts.trace import TraceProjectionV2
from assurance_quality.operations.assessment import MaterializeAssessmentHandler
from assurance_quality.operations.inspect import build_failure_classification_facts
from assurance_quality.operations.obligations import assess_obligations
from tests.acg_plan_fixture import install_plan
from tests.capabilities.conformance import execute_task

CHANGE_ID = "CH-ASSESS-001"
BATCH_ID = "20260905T010203Z"
EXECUTED_AT = datetime(2026, 9, 5, 1, 2, 3, tzinfo=UTC)
CASE_PATH = "qa/cases/items/case.yaml"
MAPPING_PATH = "qa/results/codegen/closed-mapping.json"
EVIDENCE_PATH = "qa/results/execution/execute-result.json"
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


def _workspace_input(
    root: Path,
    *,
    capability_leafs: tuple[str, ...] = (CAPABILITY, "entities.item.constraints.description"),
    journeys: tuple[str, ...] = (),
    minimum_required_coverage: Mapping[str, object] | None = None,
    case_entries: list[dict[str, object]] | None = None,
    matrix_rows: list[dict[str, object]] | None = None,
    family: Literal["api", "e2e"] = "api",
    candidates: tuple[TestFamily, ...] | None = None,
    result_status: Literal["passed", "failed"] = "passed",
    result_message: str = "",
    selector: str | None = None,
    evidence_selector: str | None = None,
) -> dict[str, Any]:
    plan_document, plan_ref = install_plan(
        root,
        CHANGE_ID,
        capability_leafs=capability_leafs,
        journeys=journeys,
        minimum_required_coverage=(
            minimum_required_coverage
            if minimum_required_coverage is not None
            else {"api": [CAPABILITY, "entities.item.constraints.description"]}
        ),
        candidates=candidates or (family,),
        proposed=candidates or (family,),
    )
    selected_cases = (
        case_entries
        if case_entries is not None
        else [
            _case("TC_ITEM_001", CAPABILITY),
            _case("TC_ITEM_002", "entities.item.constraints.description"),
        ]
    )
    selector = (selector or TEST_SELECTOR).replace("/api/", f"/{family}/")
    evidence_selector = (evidence_selector or selector).replace("/api/", f"/{family}/")
    mapped_capability = next(iter(cast(dict[str, object], selected_cases[0]["trace"])))
    preparation = _write_text(root, "qa/requirement.md", "# Requirement\n")
    review = _write_json(
        root,
        "qa/results/review/case-review.json",
        {"decision": "approved"},
    )
    case_ref = _write_text(
        root,
        CASE_PATH,
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "added": selected_cases,
                "modified": [],
                "removed": [],
            },
            sort_keys=True,
        ),
    )
    matrix_ref = _write_json(
        root,
        "qa/results/trace/minimum-coverage-matrix.json",
        matrix_rows
        if matrix_rows is not None
        else [
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
        f"qa/results/generated/{family}/files/tests/{family}/test_items.py",
        "def test_create_item():\n    assert True\n",
    )
    plan = _write_text(root, f"qa/results/plans/{family}-plan.md", "# Plan\n")
    mapping = {
        "schema_version": "1",
        "selected": [selector],
        "mappings": [
            {
                "test": selector,
                "case_id": "TC_ITEM_001",
                "capability": mapped_capability,
                "layer": family,
            }
        ],
    }
    evidence_mapping = {
        "schema_version": "1",
        "selected": [evidence_selector],
        "mappings": [
            {
                "test": evidence_selector,
                "case_id": "TC_ITEM_001",
                "capability": mapped_capability,
                "layer": family,
            }
        ],
    }
    mapping_ref = _write_json(root, MAPPING_PATH, mapping)
    evidence = {
        "schema_version": "1",
        "status": result_status,
        "change_id": CHANGE_ID,
        "batch_id": BATCH_ID,
        "executed_at": EXECUTED_AT.isoformat(),
        "plan_digest": plan_document.plan_digest,
        "plan_ref": plan_ref,
        "selected_targets": {
            "api": family == "api",
            "e2e": family == "e2e",
            "fuzz": False,
            "performance": False,
        },
        "family_outcomes": [{"family": family, "state": "executed"}],
        "mapping": evidence_mapping,
        "mapping_digest": mapping_ref["digest"],
        "baseline_tree_id": "b" * 64,
        "runner_profile_digest": "c" * 64,
        "receipt_digest": "d" * 64,
        "receipt": {
            "commands": [
                {
                    "family": family,
                    **{
                        "command": ["pytest", evidence_selector],
                        "exit_code": 0 if result_status == "passed" else 1,
                        "collected": 1,
                        "passed": 1 if result_status == "passed" else 0,
                        "failed": 1 if result_status == "failed" else 0,
                        "skipped": 0,
                    },
                }
            ]
        },
        "results": [
            {
                "test": evidence_selector,
                "status": result_status,
                "duration_ms": 5,
                "case_id": "TC_ITEM_001",
                "message": result_message,
            }
        ],
    }
    evidence_ref = _write_json(root, EVIDENCE_PATH, evidence)
    inventory_path = root / "qa/results/explore/impact-inventory.json"
    inventory_ref = {
        "path": "qa/results/explore/impact-inventory.json",
        "digest": hashlib.sha256(inventory_path.read_bytes()).hexdigest(),
    }
    selection_ref = _write_json(
        root,
        "qa/results/cases/epochs/3/selection.json",
        {
            "schema_version": "1",
            "change_id": CHANGE_ID,
            "coverage_epoch": 3,
            "plan_digest": plan_document.plan_digest,
            "inventory_ref": inventory_ref,
            "cases": [
                {
                    "case_id": item["case_id"],
                    "origin": "added",
                    "source_ref": case_ref,
                    "source_locator": CASE_PATH,
                    "mrc_ids": [CAPABILITY],
                }
                for item in selected_cases
            ],
        },
    )
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
        "selection_ref": selection_ref,
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
            "final_status": "PASS" if result_status == "passed" else "FAIL",
            "evidence_ref": evidence_ref,
            "mapping_ref": mapping_ref,
            "source_refs": [source],
            "receipt": {"receipt_id": "execute", "receipt_digest": "e" * 64},
            "family_outcomes": [{"family": family, "state": "executed"}],
        },
        "policy_resource_id": "assurance.product.configuration.product-policy",
        "policy_sha256": plan_document.policy_digest,
        "execution_at": EXECUTED_AT.isoformat(),
    }


_DURABLE_SELECTOR = "qa/tests/api/test_items.py::test_create_item"


@pytest.mark.asyncio
async def test_materialize_accepts_durable_qa_tests_mapping(tmp_path: Path) -> None:
    request = _workspace_input(tmp_path, selector=_DURABLE_SELECTOR)

    result = await execute_task(MaterializeAssessmentHandler(), request, tmp_path)

    assert result.status == "succeeded", result.failure


@pytest.mark.asyncio
async def test_materialize_rejects_view_mapping_against_durable_generation(tmp_path: Path) -> None:
    request = _workspace_input(
        tmp_path,
        selector=_DURABLE_SELECTOR,
        evidence_selector=TEST_SELECTOR,
    )

    result = await execute_task(MaterializeAssessmentHandler(), request, tmp_path)

    assert result.status == "failed"
    assert result.failure is not None
    assert result.failure.kind == "invalid_input"
    assert result.failure.message == "execution evidence mapping differs from the locked generation mapping"


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
async def test_materializes_owned_observations_and_evidence_bundle_for_failed_execution(
    tmp_path: Path,
) -> None:
    request = _workspace_input(
        tmp_path,
        result_status="failed",
        result_message="500 internal server error",
    )

    result = await execute_task(MaterializeAssessmentHandler(), request, tmp_path)

    assert result.status == "succeeded", result.failure
    output = AssessmentInputsV1.model_validate(result.output)
    observations = ObservationDocument.model_validate_json(
        result.workspace_bytes[output.observations_ref.path]
    )
    manifest = IssueEvidenceManifest.model_validate_json(
        result.workspace_bytes[output.issue_evidence_manifest_ref.path]
    )
    assert observations.change_id == CHANGE_ID
    assert observations.batch_id == BATCH_ID
    assert len(observations.observations) == 1
    assert observations.observations[0].kind == "test_failure"
    assert observations.observations[0].target == "api"
    assert output.owned_evidence_ids == (observations.observations[0].observation_id,)
    assert output.evidence_bundle_digest == manifest.digest
    manifest_refs = {entry.path: entry.digest for entry in manifest.entries}
    assert manifest_refs[output.observations_ref.path] == f"sha256:{output.observations_ref.digest}"
    assert manifest_refs[EVIDENCE_PATH] == f"sha256:{request['execution']['evidence_ref']['digest']}"
    for ref in (
        *request["generation"]["source_refs"],
        *request["reviewed_case"]["case_refs"],
        *request["reviewed_case"]["preparation_refs"],
    ):
        assert manifest_refs[ref["path"]] == f"sha256:{ref['digest']}"


def _matrix_row(
    key: str,
    sequence: int,
    *,
    category: str = "api",
    case_ids: tuple[str, ...] = ("TC_ITEM_001",),
) -> dict[str, object]:
    return {
        "mrc_id": f"MRC-{category.upper()}-{sequence:03d}",
        "key": key,
        "category": category,
        "required": True,
        "layer": "e2e" if category == "e2e" else "api",
        "covered_by_cases": list(case_ids),
        "status": "covered",
    }


@pytest.mark.asyncio
async def test_unmapped_required_api_operation_remains_a_repairable_evidence_gap(tmp_path: Path) -> None:
    capability = "operations.create_item"
    request = _workspace_input(
        tmp_path,
        capability_leafs=(capability,),
        minimum_required_coverage={"api": ["create_item", "delete_item"]},
        case_entries=[_case("TC_ITEM_001", capability)],
        matrix_rows=[_matrix_row("create_item", 1)],
    )

    result = await execute_task(MaterializeAssessmentHandler(), request, tmp_path)

    assert result.status == "succeeded", result.failure
    output = AssessmentInputsV1.model_validate(result.output)
    metrics = MetricsDocument.model_validate(json.loads(result.workspace_bytes[output.metrics_ref.path]))
    sufficiency = TraceSufficiencyFacts.model_validate(
        json.loads(result.workspace_bytes[output.sufficiency_ref.path])
    )
    assert sufficiency.sufficient is False
    assert sufficiency.insufficient_cases == ()
    assert output.scope.applicable_goals == ()
    assert (
        classify_coverage_state(
            metrics=metrics, sufficiency=sufficiency, scope=output.scope, policy=output.policy
        )
        == "repair_required"
    )
    execution = ExecutionEvidenceV1.model_validate(
        json.loads((tmp_path / EVIDENCE_PATH).read_bytes()),
        context={"case_ids": frozenset({"TC_ITEM_001"}), "capability_leafs": frozenset({capability})},
    )
    failure_facts, _ = build_failure_classification_facts(execution, metrics)
    assert (
        classify_inspection_disposition(facts=failure_facts, coverage_state="repair_required")
        == "coverage_insufficient"
    )
    gaps = json.loads(result.workspace_bytes[output.gaps_ref.path])
    assert gaps["minimum_coverage"]["summary"]["total_required"] == 2
    assert [(item["key"], item["status"]) for item in gaps["minimum_coverage"]["items"]] == [
        ("create_item", "covered"),
        ("delete_item", "missing"),
    ]
    CoverageGapsDocument.model_validate(gaps)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("first_key", "second_key", "goal"),
    [
        (CAPABILITY, "entities.item.constraints.description", "constraint_coverage"),
        ("auth_matrix.owner_read", "auth_matrix.visitor_read", "auth_matrix_coverage"),
    ],
)
async def test_unrelated_passing_case_cannot_cover_a_second_closed_obligation(
    tmp_path: Path, first_key: str, second_key: str, goal: MetricKey
) -> None:
    request = _workspace_input(
        tmp_path,
        capability_leafs=(first_key, second_key),
        minimum_required_coverage={"api": [first_key, second_key]},
        case_entries=[_case("TC_ITEM_001", first_key)],
        matrix_rows=[_matrix_row(first_key, 1), _matrix_row(second_key, 2)],
    )

    result = await execute_task(MaterializeAssessmentHandler(), request, tmp_path)

    assert result.status == "succeeded", result.failure
    output = AssessmentInputsV1.model_validate(result.output)
    metrics = MetricsDocument.model_validate(json.loads(result.workspace_bytes[output.metrics_ref.path]))
    metric = metrics.metrics[goal]
    assert metric.value == 0.5
    assert metric.declared is not None
    assert metric.declared.total == 2
    assert metric.declared.uncovered == (second_key,)
    sufficiency = TraceSufficiencyFacts.model_validate(
        json.loads(result.workspace_bytes[output.sufficiency_ref.path])
    )
    assert (
        classify_coverage_state(
            metrics=metrics, sufficiency=sufficiency, scope=output.scope, policy=output.policy
        )
        == "repair_required"
    )


@pytest.mark.asyncio
async def test_reviewed_required_operation_addition_retains_its_execution_gap(tmp_path: Path) -> None:
    optional = _case("TC_ITEM_002", CAPABILITY)
    optional["risk"] = {"level": "low", "likelihood": 1, "impact": 1, "rationale": "small impact"}
    cast(dict[str, object], optional["automation"])["required"] = False
    request = _workspace_input(
        tmp_path,
        minimum_required_coverage={"api": ["create_item"]},
        case_entries=[_case("TC_ITEM_001", CAPABILITY), optional],
        matrix_rows=[
            _matrix_row("create_item", 1),
            _matrix_row("delete_item", 2, case_ids=("TC_ITEM_002",)),
        ],
    )

    result = await execute_task(MaterializeAssessmentHandler(), request, tmp_path)

    assert result.status == "succeeded", result.failure
    output = AssessmentInputsV1.model_validate(result.output)
    sufficiency = TraceSufficiencyFacts.model_validate(
        json.loads(result.workspace_bytes[output.sufficiency_ref.path])
    )
    assert sufficiency.sufficient is False
    assert sufficiency.insufficient_cases == ()
    gaps = json.loads(result.workspace_bytes[output.gaps_ref.path])
    assert [(item["key"], item["status"]) for item in gaps["minimum_coverage"]["items"]] == [
        ("create_item", "covered"),
        ("delete_item", "not_executed"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["entities.item.constraints.description", "auth.owner_read"])
async def test_reviewed_case_trace_additions_cannot_disappear_without_matrix_rows(
    tmp_path: Path, key: str
) -> None:
    request = _workspace_input(
        tmp_path,
        capability_leafs=(CAPABILITY, key),
        minimum_required_coverage={"api": ["create_item"]},
        case_entries=[_case("TC_ITEM_001", CAPABILITY), _case("TC_ITEM_002", key)],
        matrix_rows=[_matrix_row("create_item", 1)],
    )

    result = await execute_task(MaterializeAssessmentHandler(), request, tmp_path)

    assert result.status == "succeeded", result.failure
    output = AssessmentInputsV1.model_validate(result.output)
    metrics = MetricsDocument.model_validate(json.loads(result.workspace_bytes[output.metrics_ref.path]))
    goal: MetricKey = "auth_matrix_coverage" if key.startswith("auth.") else "constraint_coverage"
    metric = metrics.metrics[goal]
    assert metric.status == "evaluated"
    assert metric.declared is not None
    assert metric.declared.uncovered == (key,)
    assert metric.value == (0.0 if goal == "auth_matrix_coverage" else 0.5)


@pytest.mark.asyncio
async def test_reviewed_trace_cannot_introduce_an_unknown_closed_key(tmp_path: Path) -> None:
    case = _case("TC_ITEM_001", CAPABILITY)
    cast(dict[str, object], case["trace"])["auth.unknown"] = {"covered": True}
    request = _workspace_input(
        tmp_path,
        minimum_required_coverage={"api": [CAPABILITY]},
        case_entries=[case],
        matrix_rows=[_matrix_row(CAPABILITY, 1)],
    )

    result = await execute_task(MaterializeAssessmentHandler(), request, tmp_path)

    assert result.status == "failed"
    assert result.failure is not None
    assert "auth.unknown" in result.failure.message


@pytest.mark.asyncio
@pytest.mark.parametrize("category", ["negative", "data_integrity"])
async def test_reviewed_closed_category_cannot_introduce_a_free_form_operation(
    tmp_path: Path, category: str
) -> None:
    request = _workspace_input(
        tmp_path,
        minimum_required_coverage={"api": ["create_item"]},
        case_entries=[_case("TC_ITEM_001", CAPABILITY)],
        matrix_rows=[
            _matrix_row("create_item", 1),
            _matrix_row("unknown_operation", 1, category=category),
        ],
    )

    result = await execute_task(MaterializeAssessmentHandler(), request, tmp_path)

    assert result.status == "failed"
    assert result.failure is not None
    assert "unknown_operation" in result.failure.message


@pytest.mark.asyncio
async def test_coverage_gap_mrc_diagnostics_must_belong_to_the_same_change(tmp_path: Path) -> None:
    result = await execute_task(MaterializeAssessmentHandler(), _workspace_input(tmp_path), tmp_path)
    assert result.status == "succeeded", result.failure
    output = AssessmentInputsV1.model_validate(result.output)
    gaps = json.loads(result.workspace_bytes[output.gaps_ref.path])
    gaps["minimum_coverage"]["change_id"] = "ANOTHER-CHANGE"

    with pytest.raises(ValidationError, match="minimum coverage.*change"):
        CoverageGapsDocument.model_validate(gaps)


@pytest.mark.asyncio
@pytest.mark.parametrize("mapping", ["unknown_journey", "unmapped_case"])
async def test_reviewed_e2e_case_requires_a_known_journey_mapping(tmp_path: Path, mapping: str) -> None:
    case = _case("TC_ITEM_001", CAPABILITY)
    case["type"] = "E2E"
    cast(dict[str, object], case["automation"])["framework"] = "pytest-playwright"
    matrix = _matrix_row("unknown" if mapping == "unknown_journey" else "checkout", 1, category="e2e")
    if mapping == "unmapped_case":
        matrix.update(
            {"covered_by_cases": [], "status": "skipped_by_scope", "required": False, "skip_reason": "none"}
        )
    request = _workspace_input(
        tmp_path,
        family="e2e",
        journeys=("checkout",),
        minimum_required_coverage={"e2e": ["checkout"]},
        case_entries=[case],
        matrix_rows=[matrix],
    )

    result = await execute_task(MaterializeAssessmentHandler(), request, tmp_path)

    assert result.status == "failed"
    assert result.failure is not None
    assert "journey" in result.failure.message.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("case_count", [1, 2])
@pytest.mark.parametrize("unmapped_journey", [False, True])
async def test_known_journey_is_one_obligation_but_each_required_e2e_case_needs_evidence(
    tmp_path: Path, case_count: int, unmapped_journey: bool
) -> None:
    cases = [_case(f"TC_ITEM_00{index}", CAPABILITY) for index in range(1, case_count + 1)]
    for case in cases:
        case["type"] = "E2E"
        cast(dict[str, object], case["automation"])["framework"] = "pytest-playwright"
    journeys = ("checkout", "refund") if unmapped_journey else ("checkout",)
    request = _workspace_input(
        tmp_path,
        family="e2e",
        journeys=journeys,
        minimum_required_coverage={"e2e": list(journeys)},
        case_entries=cases,
        matrix_rows=[
            _matrix_row(
                "checkout", 1, category="e2e", case_ids=tuple(cast(str, case["case_id"]) for case in cases)
            )
        ],
    )

    result = await execute_task(MaterializeAssessmentHandler(), request, tmp_path)

    assert result.status == "succeeded", result.failure
    output = AssessmentInputsV1.model_validate(result.output)
    metrics = MetricsDocument.model_validate(json.loads(result.workspace_bytes[output.metrics_ref.path]))
    journey = metrics.metrics["journey_coverage"]
    assert journey.declared is not None
    assert journey.declared.total == len(journeys)
    assert journey.declared.uncovered == (("refund",) if unmapped_journey else ())
    assert journey.value == (0.5 if unmapped_journey else 1.0)
    sufficiency = TraceSufficiencyFacts.model_validate(
        json.loads(result.workspace_bytes[output.sufficiency_ref.path])
    )
    assert sufficiency.sufficient is (case_count == 1)
    assert tuple(item.case_id for item in sufficiency.insufficient_cases) == (
        () if case_count == 1 else ("TC_ITEM_002",)
    )
    assert classify_coverage_state(
        metrics=metrics, sufficiency=sufficiency, scope=output.scope, policy=output.policy
    ) == ("satisfied" if case_count == 1 and not unmapped_journey else "repair_required")


@pytest.mark.asyncio
async def test_passing_e2e_trace_cannot_override_reviewed_api_obligation_layer(tmp_path: Path) -> None:
    e2e_case = _case("TC_ITEM_001", CAPABILITY)
    e2e_case["type"] = "E2E"
    cast(dict[str, object], e2e_case["automation"])["framework"] = "pytest-playwright"
    api_case = _case("TC_ITEM_002", CAPABILITY)
    api_case["risk"] = {"level": "low", "likelihood": 1, "impact": 1, "rationale": "small impact"}
    cast(dict[str, object], api_case["automation"])["required"] = False
    api_row = _matrix_row(CAPABILITY, 1, case_ids=("TC_ITEM_002",))
    api_row["required"] = False
    request = _workspace_input(
        tmp_path,
        family="e2e",
        journeys=("checkout",),
        minimum_required_coverage={"e2e": ["checkout"]},
        case_entries=[e2e_case, api_case],
        matrix_rows=[_matrix_row("checkout", 1, category="e2e"), api_row],
    )

    result = await execute_task(MaterializeAssessmentHandler(), request, tmp_path)

    assert result.status == "succeeded", result.failure
    output = AssessmentInputsV1.model_validate(result.output)
    metrics = MetricsDocument.model_validate(json.loads(result.workspace_bytes[output.metrics_ref.path]))
    assert metrics.metrics["constraint_coverage"].value == 0.0
    gaps = json.loads(result.workspace_bytes[output.gaps_ref.path])
    api_item = next(item for item in gaps["minimum_coverage"]["items"] if item["key"] == CAPABILITY)
    assert api_item["case_ids"] == ["TC_ITEM_002"]


@pytest.mark.asyncio
async def test_api_obligation_accepts_fuzz_as_supplemental_case_evidence(tmp_path: Path) -> None:
    api_case = _case("TC_ITEM_001", CAPABILITY)
    fuzz_case = _case("TC_ITEM_FUZZ_001", CAPABILITY)
    fuzz_case["type"] = "Fuzz"
    fuzz_case["related_cases"] = ["TC_ITEM_001"]
    fuzz_case["automation"] = {
        "required": True,
        "framework": "schemathesis",
        "status": "automated",
        "fuzz": {
            "endpoints": [{"method": "POST", "path": "/api/v1/items"}],
            "property": "item.create.payload",
            "expectations": ["no generated input returns a 5xx response"],
        },
    }
    request = _workspace_input(
        tmp_path,
        candidates=("api", "fuzz"),
        minimum_required_coverage={"api": ["create_item"]},
        case_entries=[api_case, fuzz_case],
        matrix_rows=[
            _matrix_row(
                "create_item",
                1,
                case_ids=("TC_ITEM_001", "TC_ITEM_FUZZ_001"),
            )
        ],
    )

    result = await execute_task(MaterializeAssessmentHandler(), request, tmp_path)

    assert result.status == "succeeded", result.failure
    output = AssessmentInputsV1.model_validate(result.output)
    gaps = CoverageGapsDocument.model_validate(json.loads(result.workspace_bytes[output.gaps_ref.path]))
    assert gaps.minimum_coverage is not None
    obligation = next(item for item in gaps.minimum_coverage.items if item.key == "create_item")
    assert obligation.case_ids == ("TC_ITEM_001", "TC_ITEM_FUZZ_001")


@pytest.mark.asyncio
async def test_optional_empty_matrix_row_cannot_hide_reviewed_trace_obligation(tmp_path: Path) -> None:
    description = "entities.item.constraints.description"
    optional_case = _case("TC_ITEM_002", description)
    optional_case["risk"] = {"level": "low", "likelihood": 1, "impact": 1, "rationale": "small impact"}
    cast(dict[str, object], optional_case["automation"])["required"] = False
    request = _workspace_input(
        tmp_path,
        minimum_required_coverage={"api": ["create_item"]},
        case_entries=[_case("TC_ITEM_001", CAPABILITY), optional_case],
        matrix_rows=[
            _matrix_row("create_item", 1),
            {
                "mrc_id": "MRC-OPTIONAL-001",
                "key": description,
                "category": "api",
                "layer": "e2e",
                "required": False,
                "covered_by_cases": [],
                "status": "skipped_by_scope",
                "skip_reason": "optional browser check",
            },
        ],
    )

    result = await execute_task(MaterializeAssessmentHandler(), request, tmp_path)

    assert result.status == "succeeded", result.failure
    output = AssessmentInputsV1.model_validate(result.output)
    metrics = MetricsDocument.model_validate(json.loads(result.workspace_bytes[output.metrics_ref.path]))
    assert metrics.metrics["constraint_coverage"].value == 0.5
    sufficiency = TraceSufficiencyFacts.model_validate(
        json.loads(result.workspace_bytes[output.sufficiency_ref.path])
    )
    assert (
        classify_coverage_state(
            metrics=metrics, sufficiency=sufficiency, scope=output.scope, policy=output.policy
        )
        == "repair_required"
    )


def _both_layer_input(root: Path, key: str, e2e_evidence: str) -> dict[str, Any]:
    e2e_trace = CAPABILITY if e2e_evidence != "unmapped" else "operations.checkout"
    e2e_case = _case("TC_ITEM_002", e2e_trace)
    e2e_case["type"] = "E2E"
    cast(dict[str, object], e2e_case["automation"])["framework"] = "pytest-playwright"
    both_row = _matrix_row(
        key, 1, case_ids=("TC_ITEM_001", "TC_ITEM_002") if e2e_evidence != "unmapped" else ("TC_ITEM_001",)
    )
    both_row["layer"] = "both"
    request = _workspace_input(
        root,
        candidates=("api", "e2e"),
        capability_leafs=(CAPABILITY, "operations.checkout"),
        journeys=("checkout",),
        minimum_required_coverage={"api": [{"key": key, "layer": "both"}], "e2e": ["checkout"]},
        case_entries=[_case("TC_ITEM_001", CAPABILITY), e2e_case],
        matrix_rows=[both_row, _matrix_row("checkout", 1, category="e2e", case_ids=("TC_ITEM_002",))],
    )
    selector = TEST_SELECTOR.replace("/api/", "/e2e/")
    mapping = json.loads((root / MAPPING_PATH).read_bytes())
    mapping["selected"].append(selector)
    mapping["mappings"].append(
        {"test": selector, "case_id": "TC_ITEM_002", "capability": e2e_trace, "layer": "e2e"}
    )
    mapping_ref = _write_json(root, MAPPING_PATH, mapping)
    source_ref = _write_text(
        root,
        "qa/tests/e2e/test_items.py",
        "def test_create_item():\n    assert True\n",
    )
    evidence = json.loads((root / EVIDENCE_PATH).read_bytes())
    evidence["mapping"] = mapping
    evidence["mapping_digest"] = mapping_ref["digest"]
    evidence["selected_targets"]["e2e"] = True
    evidence["family_outcomes"].append({"family": "e2e", "state": "executed"})
    skipped = e2e_evidence == "skipped"
    evidence["results"].append(
        {
            "test": selector,
            "case_id": "TC_ITEM_002",
            "status": "skipped" if skipped else "passed",
            "duration_ms": 5,
        }
    )
    evidence["receipt"]["commands"].append(
        {
            "family": "e2e",
            "command": ["pytest", selector],
            "exit_code": 0,
            "collected": 1,
            "passed": 0 if skipped else 1,
            "failed": 0,
            "skipped": 1 if skipped else 0,
        }
    )
    request["execution"]["evidence_ref"] = _write_json(root, EVIDENCE_PATH, evidence)
    for name in ("generation", "execution"):
        request[name]["mapping_ref"] = mapping_ref
        request[name]["source_refs"].append(source_ref)
    return request


@pytest.mark.asyncio
@pytest.mark.parametrize("key", (CAPABILITY, "create_item"))
@pytest.mark.parametrize(
    "e2e_evidence, expected_status",
    (("unmapped", "missing"), ("skipped", "not_executed"), ("passed", "covered")),
)
async def test_both_layer_obligation_needs_evidence_from_each_layer(
    tmp_path: Path, key: str, e2e_evidence: str, expected_status: str
) -> None:
    request = _both_layer_input(tmp_path, key, e2e_evidence)

    result = await execute_task(MaterializeAssessmentHandler(), request, tmp_path)

    assert result.status == "succeeded", result.failure
    output = AssessmentInputsV1.model_validate(result.output)
    gaps = CoverageGapsDocument.model_validate(json.loads(result.workspace_bytes[output.gaps_ref.path]))
    assert gaps.minimum_coverage is not None
    obligation = next(item for item in gaps.minimum_coverage.items if item.key == key)
    assert obligation.status == expected_status
    metrics = MetricsDocument.model_validate(json.loads(result.workspace_bytes[output.metrics_ref.path]))
    if key == CAPABILITY:
        assert metrics.metrics["constraint_coverage"].value == (1.0 if e2e_evidence == "passed" else 0.0)
    sufficiency = TraceSufficiencyFacts.model_validate(
        json.loads(result.workspace_bytes[output.sufficiency_ref.path])
    )
    assert classify_coverage_state(
        metrics=metrics, sufficiency=sufficiency, scope=output.scope, policy=output.policy
    ) == ("satisfied" if e2e_evidence == "passed" else "repair_required")


@pytest.mark.asyncio
async def test_numeric_mrc_obligations_retain_the_authenticated_risk_floor(tmp_path: Path) -> None:
    keys = tuple(f"entities.item.constraints.field_{index:02d}" for index in range(10))
    case = _case("TC_ITEM_001", keys[0])
    case["trace"] = {key: {"covered": True} for key in keys[:9]}
    request = _workspace_input(
        tmp_path,
        capability_leafs=keys,
        minimum_required_coverage={"api": list(keys)},
        case_entries=[case],
        matrix_rows=[_matrix_row(key, index) for index, key in enumerate(keys, start=1)],
    )

    result = await execute_task(MaterializeAssessmentHandler(), request, tmp_path)

    assert result.status == "succeeded", result.failure
    output = AssessmentInputsV1.model_validate(result.output)
    metrics = MetricsDocument.model_validate(json.loads(result.workspace_bytes[output.metrics_ref.path]))
    assert metrics.metrics["constraint_coverage"].value == 0.9
    sufficiency = TraceSufficiencyFacts.model_validate(
        json.loads(result.workspace_bytes[output.sufficiency_ref.path])
    )
    assert sufficiency.sufficient is True
    assert (
        classify_coverage_state(
            metrics=metrics, sufficiency=sufficiency, scope=output.scope, policy=output.policy
        )
        == "satisfied"
    )


@pytest.mark.asyncio
async def test_covered_free_form_api_obligation_can_satisfy_without_numeric_goals(tmp_path: Path) -> None:
    capability = "operations.create_item"
    request = _workspace_input(
        tmp_path,
        capability_leafs=(capability,),
        minimum_required_coverage={"api": ["create_item"]},
        case_entries=[_case("TC_ITEM_001", capability)],
        matrix_rows=[_matrix_row("create_item", 1)],
    )

    result = await execute_task(MaterializeAssessmentHandler(), request, tmp_path)

    assert result.status == "succeeded", result.failure
    output = AssessmentInputsV1.model_validate(result.output)
    assert output.scope.applicable_goals == ()
    metrics = MetricsDocument.model_validate(json.loads(result.workspace_bytes[output.metrics_ref.path]))
    sufficiency = TraceSufficiencyFacts.model_validate(
        json.loads(result.workspace_bytes[output.sufficiency_ref.path])
    )
    assert sufficiency.sufficient is True
    assert (
        classify_coverage_state(
            metrics=metrics, sufficiency=sufficiency, scope=output.scope, policy=output.policy
        )
        == "satisfied"
    )


@pytest.mark.asyncio
async def test_applicability_preserves_authenticated_goal_sources(tmp_path: Path) -> None:
    request = _workspace_input(tmp_path)

    result = await execute_task(MaterializeAssessmentHandler(), request, tmp_path)

    assert result.status == "succeeded", result.failure
    output = AssessmentInputsV1.model_validate(result.output)
    sources = {
        "qa/results/explore/exploration.json",
        ".aa/capability-catalog.json",
        ".aa/data-knowledge.yaml",
    }
    refs = {ref.path: ref for ref in output.scope.applicability_refs}
    assert sources <= refs.keys()
    for source in sources:
        assert refs[source].digest == hashlib.sha256((tmp_path / source).read_bytes()).hexdigest()


def test_materializer_claims_allow_reading_frozen_goal_sources() -> None:
    contract = TASK_ATTEMPT_CONTRACTS["materialize-assessment-inputs"]
    assert isinstance(contract.resources, ResourceClaimTemplate)
    claims = contract.resources.resolve(
        {
            "reviewed_case": {"change_id": CHANGE_ID},
            "coverage_epoch_token": "3",
            "execution": {"batch_id": BATCH_ID},
        }
    )

    assert {".aa/capability-catalog.json", ".aa/data-knowledge.yaml"} <= set(claims.reads)


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
        "qa/results/healing/fix-proposal.json",
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


OBSERVATION_KEY = "locked_valid_password"
OBSERVATION_ID = "OBS-LOCK-1"
PLANNED_MRC = "MRC-LOCK-1"
UNPLANNED_MRC = "MRC-LOCK-2"


def _obligation_source(requirement: dict[str, str]) -> dict[str, Any]:
    return {"kind": "requirement", "artifact": requirement, "locator": "L10"}


def _prepared_obligation(mrc_id: str, requirement: dict[str, str]) -> dict[str, Any]:
    source = _obligation_source(requirement)
    return {
        "mrc_id": mrc_id,
        "key": CAPABILITY,
        "proposed_key": None,
        "category": "api",
        "layer": "api",
        "statement": "the account locks after five failed logins",
        "applicability_conditions": [],
        "expected_basis_refs": [{"source": source, "source_status": "authenticated"}],
        "impact_row_ids": [],
        "required": True,
        "scope_disposition": "included",
        "exclusion_basis": None,
        "open_questions": [],
        "verification_requirements": [
            {
                "requirement_id": f"REQ-{mrc_id}",
                "profile_id": "api-default",
                "prerequisites": [],
                "observations": [
                    {
                        "observation_key": OBSERVATION_KEY,
                        "condition": "valid password after five failures",
                        "predicate": "status_code_eq",
                        "expected": 423,
                        "basis_refs": [source],
                    }
                ],
                "semantic_review_required": True,
                "subject_binding_required": False,
            }
        ],
    }


def _with_two_obligations(root: Path) -> dict[str, Any]:
    """One obligation carries a reviewed method plan; the other carries none."""

    request = _workspace_input(root)
    requirement = next(
        ref for ref in request["reviewed_case"]["preparation_refs"] if ref["path"] == "qa/requirement.md"
    )
    obligations = [
        _prepared_obligation(PLANNED_MRC, requirement),
        _prepared_obligation(UNPLANNED_MRC, requirement),
    ]
    goals_ref = _write_json(
        root,
        "qa/results/intake/quality-goals.json",
        {"minimum_required_coverage": obligations},
    )
    request["reviewed_case"]["preparation_refs"] = sorted(
        [*request["reviewed_case"]["preparation_refs"], goals_ref],
        key=lambda item: (item["path"], item["digest"]),
    )
    request["generation"]["reviewed_case"] = request["reviewed_case"]
    method_plan_ref = _write_json(
        root,
        "qa/results/plans/obligation-method-plans.json",
        {
            "method_plans": [
                {
                    "mrc_id": PLANNED_MRC,
                    "requirement_id": f"REQ-{PLANNED_MRC}",
                    "profile_id": "api-default",
                    "case_ids": ["TC_ITEM_001"],
                    "prerequisites": [],
                    "steps": [{"step_id": "S1", "purpose": "observe", "action": "post the valid password"}],
                    "observations": [
                        {
                            "observation_id": OBSERVATION_ID,
                            "observation_key": OBSERVATION_KEY,
                            "step_id": "S1",
                            "test_nodeid": TEST_SELECTOR,
                            "assertion_id": "A1",
                        }
                    ],
                }
            ],
            "semantic_reviews": [
                {
                    "frozen_plan_digest": request["plan_digest"],
                    "mrc_id": PLANNED_MRC,
                    "requirement_id": f"REQ-{PLANNED_MRC}",
                    "plan_ref": request["plan_ref"],
                    "status": "pass",
                    "reason": "the expected lockout status is quoted from the requirement",
                    "source_refs": [_obligation_source(requirement)],
                    "expectation_reviews": [
                        {
                            "observation_key": OBSERVATION_KEY,
                            "status": "pass",
                            "reason": "423 is the quoted locked status",
                            "basis_refs": [_obligation_source(requirement)],
                        }
                    ],
                }
            ],
        },
    )
    request["generation"]["plan_refs"] = [*request["generation"]["plan_refs"], method_plan_ref]
    return request


def test_each_obligation_is_concluded_from_its_own_method_and_observations(tmp_path: Path) -> None:
    request = MaterializeAssessmentInputV1.model_validate(_with_two_obligations(tmp_path))

    assessment = assess_obligations(workspace=tmp_path, request=request)

    verdicts = {row.mrc_id: row for row in assessment.rows}
    assert set(verdicts) == {PLANNED_MRC, UNPLANNED_MRC}
    # The whole run passed, but neither obligation has a runtime observation for
    # its own required key, so neither may be reported as supported.
    assert verdicts[PLANNED_MRC].verdict == "inconclusive"
    assert "obligation_observation_missing" in verdicts[PLANNED_MRC].gap_codes
    # The second obligation has no reviewed method at all, which is a different
    # gap from a missing observation.
    assert verdicts[UNPLANNED_MRC].verdict == "inconclusive"
    assert "obligation_case_missing" in verdicts[UNPLANNED_MRC].gap_codes
    assert "obligation_case_missing" not in verdicts[PLANNED_MRC].gap_codes


def test_unreviewed_expectation_is_not_confirmed(tmp_path: Path) -> None:
    payload = _with_two_obligations(tmp_path)
    plans = json.loads((tmp_path / "qa/results/plans/obligation-method-plans.json").read_bytes())
    plans["semantic_reviews"][0]["status"] = "abstain"
    ref = _write_json(tmp_path, "qa/results/plans/obligation-method-plans.json", plans)
    payload["generation"]["plan_refs"] = [
        item for item in payload["generation"]["plan_refs"] if item["path"] != ref["path"]
    ] + [ref]
    request = MaterializeAssessmentInputV1.model_validate(payload)

    assessment = assess_obligations(workspace=tmp_path, request=request)

    row = next(item for item in assessment.rows if item.mrc_id == PLANNED_MRC)
    assert row.verdict == "inconclusive"
    assert "expectation_unconfirmed" in row.gap_codes


def _with_runtime_observations(root: Path, *, predicate_passed: bool) -> dict[str, Any]:
    payload = _with_two_obligations(root)
    mapping_ref = payload["execution"]["mapping_ref"]
    bundle_ref = _write_json(
        root,
        f"qa/results/execution/epochs/3/batches/{BATCH_ID}/runtime-observations.json",
        {
            "schema_version": "1",
            "identity": {
                "plan_digest": payload["plan_digest"],
                "method_plan_refs": [payload["generation"]["plan_refs"][-1]],
                "mapping_digest": mapping_ref["digest"],
                "batch_id": BATCH_ID,
                "baseline_tree_id": "b" * 64,
                "runner_profile_digest": "c" * 64,
            },
            "subject": {
                "kind": "local",
                "expected_identity": "loopback",
                "observed_identity": "loopback",
                "evidence_ref": None,
                "status": "matched",
            },
            "observations": [
                {
                    "observation_id": OBSERVATION_ID,
                    "observation_key": OBSERVATION_KEY,
                    "mrc_id": PLANNED_MRC,
                    "requirement_id": f"REQ-{PLANNED_MRC}",
                    "test_nodeid": TEST_SELECTOR,
                    "assertion_id": "A1",
                    "step_id": "S1",
                    "sequence_id": "SEQ-1",
                    "sequence_index": 0,
                    "actual_status": 423 if predicate_passed else 200,
                    "predicate_passed": predicate_passed,
                    "observed_at": EXECUTED_AT.isoformat(),
                    "prerequisite_refs": [],
                }
            ],
            "collection_errors": [],
        },
    )
    payload["execution"]["observations_ref"] = bundle_ref
    return payload


@pytest.mark.parametrize(
    ("predicate_passed", "expected"),
    ((True, "supported"), (False, "refuted")),
)
def test_obligation_verdict_follows_its_own_runtime_observation(
    tmp_path: Path, predicate_passed: bool, expected: str
) -> None:
    request = MaterializeAssessmentInputV1.model_validate(
        _with_runtime_observations(tmp_path, predicate_passed=predicate_passed)
    )

    assessment = assess_obligations(workspace=tmp_path, request=request)

    rows = {row.mrc_id: row for row in assessment.rows}
    assert rows[PLANNED_MRC].verdict == expected
    # The unplanned obligation shares the run but has no observation of its own,
    # so the same execution cannot conclude it either way.
    assert rows[UNPLANNED_MRC].verdict == "inconclusive"
