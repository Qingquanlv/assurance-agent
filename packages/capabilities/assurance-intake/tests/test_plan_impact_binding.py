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


def test_same_row_id_with_different_inventory_digest_is_rejected(tmp_path: Path) -> None:
    first, _ = install_plan(
        tmp_path / "a",
        "CH-1",
        journeys=("dept_management_crud",),
        candidates=("api", "e2e"),
        proposed=("api",),
        impact_rows=(_journey_row(),),
    )
    second, _ = install_plan(
        tmp_path / "b",
        "CH-1",
        journeys=("dept_management_crud",),
        candidates=("api", "e2e"),
        proposed=("api",),
        impact_rows=({**_journey_row(), "confidence": "high"},),
    )
    assert first.impact_inventory_ref.digest != second.impact_inventory_ref.digest
    assert first.impact_inventory_ref.path == second.impact_inventory_ref.path


def test_api_only_excludes_e2e_obligation_when_run_spec_is_authenticated(tmp_path: Path) -> None:
    snapshot = tmp_path / "qa/results/intake/sources/run-spec.effective.yaml"
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    snapshot.write_text("candidate_test_families:\n- api\n", encoding="utf-8")
    plan, _ = install_plan(
        tmp_path,
        "CH-1",
        journeys=("dept_management_crud",),
        candidates=("api",),
        proposed=("api",),
        minimum_required_coverage=[
            {
                "draft_id": "D-API",
                "proposed_key": "entities.item.constraints.name",
                "category": "api",
                "layer": "api",
                "statement": "api holds",
                "applicability_conditions": [],
                "impact_row_ids": [],
                "proposed_profile_id": None,
                "prerequisites": [],
                "observation_goals": [],
                "basis_quotes": [],
                "open_questions": [],
            },
            {
                "draft_id": "D-E2E",
                "proposed_key": "dept_management_crud",
                "category": "e2e",
                "layer": "e2e",
                "statement": "journey holds",
                "applicability_conditions": [],
                "impact_row_ids": [],
                "proposed_profile_id": None,
                "prerequisites": [],
                "observation_goals": [],
                "basis_quotes": [],
                "open_questions": [],
            },
        ],
    )
    assert plan.selected_test_families == ("api",)
    assert "e2e" not in plan.quality_goal.required_test_families


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
