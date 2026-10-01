from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from assurance_intake.contracts.impact import (
    CandidateCaseV1,
    ChangeEvidenceV1,
    ChangeImpactInventoryV1,
    HistoricalProblemV1,
    ImpactProjectionV1,
    ImpactSeedV1,
)
from assurance_intake.domain.impact_validation import (
    impact_required_families,
    validate_inventory_closed_keys,
    validate_inventory_references,
)

_LEAFS = frozenset(
    {
        "entities.dept.constraints.name_unique",
        "capabilities.domain_factories.dept.make_dept",
    }
)


def _row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "row_id": "IR-001",
        "change_evidence_ids": ["CF-001"],
        "affected_behavior": {"kind": "api", "key": "POST /api/v1/dept/create"},
        "obligation": "creating a department with a duplicate name must return 400",
        "expected_basis_ids": ["SC-001"],
        "assets": {"case_ids": [], "factory_leafs": [], "problem_ids": []},
        "disposition": "add",
        "gap_reason": None,
        "confidence": "medium",
    }
    row.update(overrides)
    return row


def _inventory(
    *rows: dict[str, Any], exclusions: list[dict[str, str]] | None = None
) -> ChangeImpactInventoryV1:
    return ChangeImpactInventoryV1.model_validate(
        {
            "schema_version": "1",
            "change_id": "CH-1",
            "context_ref": "explore/context.json",
            "rows": list(rows),
            "exclusions": exclusions or [],
        }
    )


def _projection() -> ImpactProjectionV1:
    return ImpactProjectionV1(
        diff_base="content-snapshot",
        seeds=(
            ImpactSeedV1(seed_id="CF-001", path="app/controllers/dept.py", reason="requirement_hint"),
            ImpactSeedV1(seed_id="CF-002", path="app/api/v1/depts/depts.py", reason="requirement_hint"),
        ),
        candidate_cases=(
            CandidateCaseV1(
                evidence_id="CS-001",
                case_id="TC_DEPT_API_001",
                module="system.dept",
                path="qa/cases/system/dept/case.yaml",
                title="create top-level department",
            ),
        ),
        historical_problems=(
            HistoricalProblemV1(evidence_id="HI-001", problem_id="PROB-1", title="500 on duplicate name"),
        ),
        factory_leafs=("capabilities.domain_factories.dept.make_dept",),
    )


def test_projection_exposes_seed_and_resolvable_ids() -> None:
    projection = _projection()
    assert projection.seed_ids() == frozenset({"CF-001", "CF-002"})
    assert projection.resolvable_ids() == frozenset(
        {"CF-001", "CF-002", "CS-001", "TC_DEPT_API_001", "HI-001", "PROB-1"}
    )


def test_projection_rejects_duplicate_identities() -> None:
    with pytest.raises(ValidationError, match="seed_id"):
        ImpactProjectionV1(
            diff_base="content-snapshot",
            seeds=(
                ImpactSeedV1(seed_id="CF-001", path="a.py", reason="requirement_hint"),
                ImpactSeedV1(seed_id="CF-001", path="b.py", reason="requirement_hint"),
            ),
            candidate_cases=(),
            historical_problems=(),
            factory_leafs=(),
        )


def test_change_evidence_requires_digest_unless_deleted() -> None:
    ChangeEvidenceV1.model_validate(
        {
            "schema_version": "1",
            "change_id": "CH-1",
            "base_ref": "abc123",
            "head_ref": "def456",
            "changed_files": [
                {
                    "path": "app/controllers/dept.py",
                    "status": "modified",
                    "symbols": ["update_dept"],
                    "digest": "a" * 64,
                },
                {"path": "app/legacy.py", "status": "deleted", "symbols": [], "digest": None},
            ],
        }
    )
    with pytest.raises(ValidationError, match="digest"):
        ChangeEvidenceV1.model_validate(
            {
                "schema_version": "1",
                "change_id": "CH-1",
                "base_ref": "abc123",
                "head_ref": "def456",
                "changed_files": [{"path": "app/controllers/dept.py", "status": "modified", "digest": None}],
            }
        )


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"disposition": "reuse"}, "requires assets.case_ids"),
        ({"disposition": "modify"}, "requires assets.case_ids"),
        (
            {
                "disposition": "add",
                "assets": {"case_ids": ["TC_DEPT_API_001"], "factory_leafs": [], "problem_ids": []},
            },
            "cannot cite existing",
        ),
        ({"disposition": "capability_gap"}, "requires gap_reason"),
        ({"disposition": "pending_confirmation"}, "requires gap_reason"),
        ({"disposition": "add", "gap_reason": "no"}, "may set gap_reason"),
    ],
)
def test_row_disposition_shape_is_enforced(overrides: dict[str, Any], match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        _inventory(_row(**overrides))


def test_inventory_rejects_duplicate_rows_and_contradictory_exclusions() -> None:
    with pytest.raises(ValidationError, match="row_id"):
        _inventory(_row(), _row())
    with pytest.raises(ValidationError, match="both cited and excluded"):
        _inventory(_row(), exclusions=[{"seed_id": "CF-001", "reason": "docs only"}])


def test_references_must_resolve_and_every_seed_must_be_handled() -> None:
    projection = _projection()
    resolvable = projection.resolvable_ids() | {"SC-001"}
    good = _inventory(
        _row(),
        _row(
            row_id="IR-002",
            change_evidence_ids=["CF-002"],
            affected_behavior={"kind": "data_constraint", "key": "entities.dept.constraints.name_unique"},
            assets={
                "case_ids": ["TC_DEPT_API_001"],
                "factory_leafs": ["capabilities.domain_factories.dept.make_dept"],
                "problem_ids": ["PROB-1"],
            },
            disposition="modify",
        ),
    )
    validate_inventory_references(
        good, resolvable=resolvable, seed_ids=projection.seed_ids(), capability_leafs=_LEAFS
    )

    with pytest.raises(ValueError, match=r"IR-001: unresolvable expected_basis_ids: \['SC-999'\]"):
        validate_inventory_references(
            _inventory(
                _row(expected_basis_ids=["SC-999"]), _row(row_id="IR-002", change_evidence_ids=["CF-002"])
            ),
            resolvable=resolvable,
            seed_ids=projection.seed_ids(),
            capability_leafs=_LEAFS,
        )
    with pytest.raises(ValueError, match=r"seeds without an impact row or exclusion: \['CF-002'\]"):
        validate_inventory_references(
            _inventory(_row()), resolvable=resolvable, seed_ids=projection.seed_ids(), capability_leafs=_LEAFS
        )
    excluded = _inventory(
        _row(),
        exclusions=[{"seed_id": "CF-002", "reason": "router wiring only; behavior lives in the controller"}],
    )
    validate_inventory_references(
        excluded, resolvable=resolvable, seed_ids=projection.seed_ids(), capability_leafs=_LEAFS
    )
    with pytest.raises(ValueError, match="exclusion cites unknown seed: CF-009"):
        validate_inventory_references(
            _inventory(
                _row(),
                _row(row_id="IR-002", change_evidence_ids=["CF-002"]),
                exclusions=[{"seed_id": "CF-009", "reason": "x"}],
            ),
            resolvable=resolvable,
            seed_ids=projection.seed_ids(),
            capability_leafs=_LEAFS,
        )


def test_data_constraint_and_factory_leafs_stay_inside_the_typed_catalog() -> None:
    projection = _projection()
    resolvable = projection.resolvable_ids() | {"SC-001"}
    with pytest.raises(ValueError, match="data_constraint key is not a typed leaf"):
        validate_inventory_references(
            _inventory(
                _row(
                    affected_behavior={"kind": "data_constraint", "key": "entities.dept.constraints.invented"}
                ),
                _row(row_id="IR-002", change_evidence_ids=["CF-002"]),
            ),
            resolvable=resolvable,
            seed_ids=projection.seed_ids(),
            capability_leafs=_LEAFS,
        )
    gap = _inventory(
        _row(
            affected_behavior={"kind": "data_constraint", "key": "entities.dept.constraints.invented"},
            disposition="capability_gap",
            gap_reason="closure depth is not a catalog leaf",
        ),
        _row(row_id="IR-002", change_evidence_ids=["CF-002"]),
    )
    validate_inventory_references(
        gap, resolvable=resolvable, seed_ids=projection.seed_ids(), capability_leafs=_LEAFS
    )
    with pytest.raises(ValueError, match="factory_leafs outside the typed catalog"):
        validate_inventory_references(
            _inventory(
                _row(
                    assets={
                        "case_ids": [],
                        "factory_leafs": ["capabilities.domain_factories.dept.nope"],
                        "problem_ids": [],
                    }
                ),
                _row(row_id="IR-002", change_evidence_ids=["CF-002"]),
            ),
            resolvable=resolvable,
            seed_ids=projection.seed_ids(),
            capability_leafs=_LEAFS,
        )


def test_journey_rows_must_name_an_authenticated_journey_unless_gap() -> None:
    journey = _row(affected_behavior={"kind": "journey", "key": "dept_management_crud"})
    validate_inventory_closed_keys(_inventory(journey), journey_keys=frozenset({"dept_management_crud"}))
    with pytest.raises(ValueError, match="IR-001: journey key is not an authenticated journey"):
        validate_inventory_closed_keys(_inventory(journey), journey_keys=frozenset())
    gap = _row(
        affected_behavior={"kind": "journey", "key": "dept_move_subtree"},
        disposition="capability_gap",
        gap_reason="journey is not declared in data knowledge",
    )
    validate_inventory_closed_keys(_inventory(gap), journey_keys=frozenset())


def test_required_families_come_only_from_closed_rows() -> None:
    inventory = _inventory(
        _row(affected_behavior={"kind": "journey", "key": "dept_management_crud"}),
        _row(
            row_id="IR-002",
            affected_behavior={"kind": "role", "key": "admin"},
            disposition="pending_confirmation",
            gap_reason="owner must confirm",
        ),
        _row(
            row_id="IR-003",
            affected_behavior={"kind": "data_constraint", "key": "entities.dept.constraints.name_unique"},
            disposition="capability_gap",
            gap_reason="no factory",
        ),
    )
    assert impact_required_families(inventory) == ("e2e",)
    assert [row.row_id for row in inventory.open_rows()] == ["IR-002", "IR-003"]
    assert [row.row_id for row in inventory.actionable_rows()] == ["IR-001"]
    assert inventory.cited_ids() >= {"CF-001", "SC-001"}
