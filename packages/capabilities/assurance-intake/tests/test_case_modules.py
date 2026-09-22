from __future__ import annotations

import pytest
from pydantic import ValidationError

from assurance_intake.contracts.impact import AffectedBehaviorV1, ChangeImpactInventoryV1, ImpactRowV1
from assurance_intake.operations.case_modules import infer_case_delta_paths, module_from_behavior


def _row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "row_id": "IR-001",
        "change_evidence_ids": ["CF-001"],
        "affected_behavior": {"kind": "api", "key": "POST /api/v1/dept/create"},
        "obligation": "creating a department with a duplicate name must return 400",
        "expected_basis_ids": [],
        "assets": {"case_ids": [], "factory_leafs": [], "problem_ids": []},
        "disposition": "add",
        "gap_reason": None,
        "confidence": "medium",
    }
    row.update(overrides)
    return row


def _inventory(*rows: dict[str, object]) -> ChangeImpactInventoryV1:
    return ChangeImpactInventoryV1.model_validate(
        {
            "schema_version": "1",
            "change_id": "CH-1",
            "context_ref": "explore/context.json",
            "rows": list(rows),
            "exclusions": [],
        }
    )


@pytest.mark.parametrize(
    ("kind", "key", "expected"),
    [
        ("api", "DELETE /api/v1/dept/delete", "dept"),
        ("api", "POST /items", "items"),
        ("api", "GET /api/dept/list", "dept"),
        ("journey", "dept_management_crud", "dept_management_crud"),
        ("role", "admin", "admin"),
        ("data_constraint", "entities.dept.constraints.name_unique", "dept"),
    ],
)
def test_module_from_behavior_projects_common_keys(kind: str, key: str, expected: str) -> None:
    assert module_from_behavior(AffectedBehaviorV1(kind=kind, key=key)) == expected  # type: ignore[arg-type]


def test_infer_prefers_explicit_case_module() -> None:
    inventory = _inventory(_row(case_module="system/dept"))
    assert infer_case_delta_paths(inventory) == ("qa/cases/system/dept/case.yaml",)


def test_infer_emits_one_case_file_per_distinct_module() -> None:
    inventory = _inventory(
        _row(case_module="system/dept"),
        _row(
            row_id="IR-002",
            change_evidence_ids=["CF-002"],
            affected_behavior={"kind": "api", "key": "POST /api/v1/user/create"},
            case_module="system/user",
        ),
    )
    assert infer_case_delta_paths(inventory) == (
        "qa/cases/system/dept/case.yaml",
        "qa/cases/system/user/case.yaml",
    )


def test_infer_collapses_rows_that_share_a_module() -> None:
    inventory = _inventory(
        _row(affected_behavior={"kind": "api", "key": "GET /api/v1/dept/list"}),
        _row(
            row_id="IR-002",
            change_evidence_ids=["CF-002"],
            affected_behavior={"kind": "api", "key": "POST /api/v1/dept/create"},
        ),
    )
    assert infer_case_delta_paths(inventory) == ("qa/cases/dept/case.yaml",)


def test_infer_uses_actionable_rows_only() -> None:
    inventory = _inventory(
        _row(
            disposition="capability_gap",
            gap_reason="no factory yet",
            affected_behavior={"kind": "api", "key": "POST /api/v1/user/create"},
        ),
        _row(
            row_id="IR-002",
            change_evidence_ids=["CF-002"],
            affected_behavior={"kind": "api", "key": "POST /api/v1/dept/create"},
        ),
    )
    assert infer_case_delta_paths(inventory) == ("qa/cases/dept/case.yaml",)


def test_infer_rejects_empty_inventory() -> None:
    inventory = _inventory()
    with pytest.raises(ValueError, match="does not imply any case module"):
        infer_case_delta_paths(inventory)


def test_infer_rejects_open_rows_without_an_actionable_module() -> None:
    inventory = _inventory(
        _row(
            disposition="capability_gap",
            gap_reason="no factory yet",
            affected_behavior={"kind": "api", "key": "POST /api/v1/user/create"},
        )
    )
    with pytest.raises(ValueError, match="does not imply any case module"):
        infer_case_delta_paths(inventory)


def test_case_module_rejects_unsafe_segments() -> None:
    with pytest.raises(ValidationError, match="case_module"):
        ImpactRowV1.model_validate(_row(case_module="../escape"))
    with pytest.raises(ValidationError, match="case_module"):
        ImpactRowV1.model_validate(_row(case_module="system/dept/"))
