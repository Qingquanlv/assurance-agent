from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.acg_plan_fixture import install_plan


def _journey_row(disposition: str = "add", gap_reason: str | None = None) -> dict[str, Any]:
    return {
        "row_id": "IR-001",
        "change_evidence_ids": ["CF-001"],
        "affected_behavior": {"kind": "journey", "key": "dept_management_crud"},
        "obligation": "the department tree page must still render after the change",
        "expected_basis_ids": [],
        "assets": {"case_ids": [], "factory_leafs": [], "problem_ids": []},
        "disposition": disposition,
        "gap_reason": gap_reason,
        "confidence": "medium",
    }


def test_sealed_plan_binds_the_inventory_and_retains_its_family(tmp_path: Path) -> None:
    plan, _ = install_plan(
        tmp_path,
        "CH-1",
        journeys=("dept_management_crud",),
        candidates=("api", "e2e"),
        proposed=("api",),
        impact_rows=(_journey_row(),),
    )
    assert plan.impact_inventory_ref.path == "qa/results/explore/impact-inventory.json"
    assert plan.selected_test_families == ("api", "e2e")
    assert [r.family for r in plan.resolution_reasons if r.reason_code == "impact_retained"] == ["e2e"]


def test_plan_preparation_rejects_journey_rows_outside_authenticated_journeys(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="IR-001: journey key is not an authenticated journey"):
        install_plan(tmp_path, "CH-1", journeys=(), impact_rows=(_journey_row(),))


def test_plan_preparation_accepts_a_declared_journey_gap(tmp_path: Path) -> None:
    plan, _ = install_plan(
        tmp_path,
        "CH-1",
        journeys=(),
        candidates=("api", "e2e"),
        proposed=("api",),
        impact_rows=(_journey_row("capability_gap", "journey is not declared in data knowledge"),),
    )
    assert plan.selected_test_families == ("api",)
