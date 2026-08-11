"""MRC artifact models — closed keys and result shape (Verification Metrics M1 Task 5)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from assurance_agent.artifacts.models.minimum_coverage import (
    MinimumCoverageItem,
    MinimumCoverageMatrix,
    MinimumCoverageMatrixRow,
    MinimumCoverageResult,
    MinimumCoverageSummary,
    MrcFinding,
    auth_known_keys,
    maps_from_advisory_mrc,
    mrc_closed_key_findings,
)


def test_result_summary_must_match_item_status_counts() -> None:
    item = MinimumCoverageItem(
        mrc_id="MRC-API-001",
        key="entities.dept.constraints.name_unique",
        category="negative",
        required=True,
        layer="api",
        status="covered",
        case_ids=("TC_DEPT_API_010",),
        executed_case_ids=("TC_DEPT_API_010",),
        mapping_source="trace",
    )
    with pytest.raises(ValidationError, match="summary"):
        MinimumCoverageResult(
            schema_version="1.0",
            change_id="CH-MRC-001",
            summary=MinimumCoverageSummary(
                total_required=1,
                covered=0,
                covered_known_issue=0,
                covered_but_failing=1,
                not_executed=0,
                missing=0,
                skipped_by_scope=0,
            ),
            items=(item,),
        )


def test_result_of_builds_matching_summary() -> None:
    result = MinimumCoverageResult.of(
        change_id="CH-MRC-001",
        items=(
            MinimumCoverageItem(
                mrc_id="MRC-NEG-001",
                key="entities.dept.constraints.name_unique",
                category="negative",
                required=True,
                layer="api",
                status="covered_but_failing",
                case_ids=("TC_A",),
                executed_case_ids=("TC_A",),
                mapping_source="trace",
            ),
            MinimumCoverageItem(
                mrc_id="MRC-E2E-001",
                key="admin_creates_dept_and_sees_in_tree",
                category="e2e_if_enabled",
                required=True,
                layer="e2e",
                status="not_executed",
                case_ids=("TC_B",),
                executed_case_ids=(),
                mapping_source="trace",
            ),
        ),
    )
    assert result.summary.total_required == 2
    assert result.summary.covered_but_failing == 1
    assert result.summary.not_executed == 1
    assert result.summary.covered == 0


def test_matrix_accepts_legacy_bare_list() -> None:
    matrix = MinimumCoverageMatrix.model_validate(
        [
            {
                "mrc_id": "create_dept",
                "key": "create_dept",
                "required": True,
                "covered_by_cases": ["TC_DEPT_API_001"],
                "status": "covered",
                "skip_reason": None,
            }
        ]
    )
    assert len(matrix.root) == 1
    assert matrix.root[0].key == "create_dept"


@pytest.mark.parametrize(
    ("duplicate_field", "duplicate_value"),
    (("key", "create_dept"), ("mrc_id", "MRC-API-001")),
)
def test_matrix_rejects_duplicate_row_identity(
    duplicate_field: str,
    duplicate_value: str,
) -> None:
    first = {
        "mrc_id": "MRC-API-001",
        "key": "create_dept",
        "required": True,
        "covered_by_cases": [],
        "status": "skipped_by_scope",
        "skip_reason": "out of scope",
    }
    second = {
        "mrc_id": "MRC-API-002",
        "key": "list_depts",
        "required": True,
        "covered_by_cases": [],
        "status": "skipped_by_scope",
        "skip_reason": "out of scope",
        duplicate_field: duplicate_value,
    }

    with pytest.raises(ValidationError, match=rf"duplicate {duplicate_field}"):
        MinimumCoverageMatrix.model_validate([first, second])


def test_auth_known_keys_from_data_knowledge_shape() -> None:
    keys = auth_known_keys(
        {
            "auth": {"admin_token": {}, "user_token": {}},
            "auth_matrix": {"cell_get_admin": {}, "cell_get_user": {}},
        }
    )
    assert keys == frozenset(
        {
            "auth.admin_token",
            "auth.user_token",
            "auth_matrix.cell_get_admin",
            "auth_matrix.cell_get_user",
        }
    )


def test_closed_key_findings_for_negative_and_journey() -> None:
    items = (
        MinimumCoverageMatrixRow(
            mrc_id="MRC-NEG-001",
            key="entities.dept.constraints.name_unique",
            required=True,
            covered_by_cases=["TC_A"],
            status="covered",
            category="negative",
        ),
        MinimumCoverageMatrixRow(
            mrc_id="MRC-NEG-002",
            key="invented_by_llm",
            required=True,
            covered_by_cases=["TC_B"],
            status="covered",
            category="negative",
        ),
        MinimumCoverageMatrixRow(
            mrc_id="MRC-E2E-001",
            key="admin_creates_dept_and_sees_in_tree",
            required=True,
            covered_by_cases=["TC_C"],
            status="covered",
            category="e2e_if_enabled",
        ),
        MinimumCoverageMatrixRow(
            mrc_id="MRC-AUTH-001",
            key="auth_matrix.cell_get_admin",
            required=True,
            covered_by_cases=["TC_D"],
            status="covered",
            category="negative",
        ),
    )
    findings = mrc_closed_key_findings(
        items,
        constraint_keys=frozenset({"entities.dept.constraints.name_unique"}),
        auth_keys=frozenset({"auth_matrix.cell_get_admin"}),
        journey_keys=frozenset({"admin_creates_dept_and_sees_in_tree"}),
    )
    assert findings == (MrcFinding(code="unknown_closed_key", key="invented_by_llm"),)


def test_maps_from_advisory_mrc_assigns_skill_shaped_ids() -> None:
    category_by_key, layer_by_key, mrc_id_by_key = maps_from_advisory_mrc(
        {
            "api": ["create_dept", "list_dept_tree"],
            "negative": ["missing_required_fields"],
            "e2e_if_enabled": ["admin_creates_dept_and_sees_in_tree"],
            "data_integrity": ["parent_child_tree_consistency"],
        }
    )
    assert category_by_key["missing_required_fields"] == "negative"
    assert layer_by_key["admin_creates_dept_and_sees_in_tree"] == "e2e"
    assert mrc_id_by_key["create_dept"] == "MRC-API-001"
    assert mrc_id_by_key["list_dept_tree"] == "MRC-API-002"
    assert mrc_id_by_key["missing_required_fields"] == "MRC-NEGATIVE-001"
    assert mrc_id_by_key["admin_creates_dept_and_sees_in_tree"] == "MRC-E2E-001"
    assert mrc_id_by_key["parent_child_tree_consistency"] == "MRC-DATA-INTEGRITY-001"
