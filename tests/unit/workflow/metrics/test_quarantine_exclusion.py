"""A2/A4 quarantine exclusion stays in denominator (M3 Task 4 gaming prevention)."""

from __future__ import annotations

from datetime import UTC, datetime

from assurance_agent.artifacts.models.minimum_coverage import MrcObligation
from assurance_agent.artifacts.models.trace import (
    TraceExecution,
    TraceProjection,
    TraceRow,
    TraceTestRef,
)
from assurance_agent.verification.property_scan import PropertyMarkerHit
from assurance_agent.workflow.execution.results import PropertyTestResult
from assurance_agent.workflow.metrics.constraint_coverage import compute_constraint_coverage
from assurance_agent.workflow.metrics.journey_coverage import compute_journey_coverage

CHANGE_ID = "CH-Q-EXCL"
BATCH = "20260805-100000"
TS = datetime(2026, 8, 5, 10, 0, 0, tzinfo=UTC)

KEY_A = "entities.dept.constraints.name_unique"
KEY_B = "entities.dept.constraints.name_has_max_length"
JOURNEY_A = "admin_creates_dept_and_sees_in_tree"
JOURNEY_B = "non_admin_cannot_access_api_management"

STRONG_API = """
import pytest

@pytest.mark.property("entities.dept.constraints.name_unique")
async def test_a():
    response = {"code": 400, "detail": "exists"}
    assert response["code"] == 400
    assert response["detail"] == "exists"

@pytest.mark.property("entities.dept.constraints.name_has_max_length")
async def test_b():
    response = {"code": 400, "detail": "too long"}
    assert response["code"] == 400
    assert response["detail"] == "too long"
"""

STRONG_E2E = """
async def test_journey():
    await page.goto("/dept")
    await page.get_by_role("button", name="Create").click()
    await expect(page.get_by_text("Dept A")).to_have_count(1)
"""


def _hit(key: str, test_name: str) -> PropertyMarkerHit:
    return PropertyMarkerHit(
        file="tests/api/test_props.py",
        test_name=test_name,
        constraint_keys=(key,),
        lineno=4,
    )


def _passed(key: str, test_name: str) -> PropertyTestResult:
    return PropertyTestResult(
        nodeid=f"tests/api/test_props.py::{test_name}",
        file="tests/api/test_props.py",
        constraint_keys=(key,),
        outcome="passed",
        batch_id=BATCH,
    )


def test_a2_one_quarantined_of_two_is_half_not_one_by_dropping_denom() -> None:
    """Quarantined key remains in declared total; covered ratio is 0.5, never 1.0."""
    evidence = compute_constraint_coverage(
        change_id=CHANGE_ID,
        batch_id=BATCH,
        known_keys=frozenset({KEY_A, KEY_B}),
        touched_entities=frozenset({"dept"}),
        property_tests=(
            _passed(KEY_A, "test_a"),
            _passed(KEY_B, "test_b"),
        ),
        marker_hits=(_hit(KEY_A, "test_a"), _hit(KEY_B, "test_b")),
        test_sources={"tests/api/test_props.py": STRONG_API},
        quarantine=(KEY_B,),
    )
    assert evidence.declared is not None
    assert evidence.declared.total == 2
    assert evidence.declared.covered == 1
    assert evidence.value == 0.5
    assert KEY_B in evidence.declared.uncovered
    assert any(
        board.code == "quarantined_excluded" and KEY_B in board.detail for board in evidence.shortboards
    )


def test_a2_quarantine_cannot_inflate_by_removing_from_known_keys() -> None:
    """Dropping quarantined keys from known_keys is not the collector's path —
    when quarantine is applied over the full closed set, denom stays 2."""
    evidence = compute_constraint_coverage(
        change_id=CHANGE_ID,
        batch_id=BATCH,
        known_keys=frozenset({KEY_A, KEY_B}),
        touched_entities=frozenset({"dept"}),
        property_tests=(_passed(KEY_A, "test_a"),),
        marker_hits=(_hit(KEY_A, "test_a"),),
        test_sources={"tests/api/test_props.py": STRONG_API},
        quarantine=(KEY_B,),
    )
    assert evidence.declared is not None
    assert evidence.declared.total == 2
    assert evidence.value == 0.5


def _row(case_id: str) -> TraceRow:
    latest = TraceExecution(
        batch_id=BATCH,
        target="e2e",
        status="passed",
        ts=TS,
        ts_source="executed_at",
    )
    return TraceRow(
        case_id=case_id,
        module="system.dept",
        case_type="E2E",
        automation_required=True,
        assertions=("ok",),
        covering_tests=(TraceTestRef(file="tests/e2e/test_dept.py", test_name="test_journey"),),
        coverage_state="covered",
        latest_execution=latest,
        freshest_pass=latest,
        presence_in_current_batch="executed",
        atemporal_kinds_present=(),
        open_problem_ids=(),
    )


def _obligation(key: str, case_id: str) -> MrcObligation:
    return MrcObligation.model_validate(
        {
            "mrc_id": f"MRC-{key}",
            "key": key,
            "category": "e2e_if_enabled",
            "required": True,
            "layer": "e2e",
            "case_ids": (case_id,),
        }
    )


def test_a4_one_quarantined_of_two_is_half_not_one_by_dropping_denom() -> None:
    projection = TraceProjection(
        schema_version="1",
        change_id=CHANGE_ID,
        phase="reconciled",
        authoritative_batch_id=BATCH,
        sources=(),
        rows=(_row("TC_E2E_001"), _row("TC_E2E_002")),
        unmapped_tests=(),
        gaps=(),
        integrity="complete",
    )
    evidence = compute_journey_coverage(
        change_id=CHANGE_ID,
        batch_id=BATCH,
        obligations=(
            _obligation(JOURNEY_A, "TC_E2E_001"),
            _obligation(JOURNEY_B, "TC_E2E_002"),
        ),
        projection=projection,
        quarantine=(JOURNEY_B,),
        test_sources={"tests/e2e/test_dept.py": STRONG_E2E},
        case_functions={
            "TC_E2E_001": ("tests/e2e/test_dept.py", "test_journey"),
            "TC_E2E_002": ("tests/e2e/test_dept.py", "test_journey"),
        },
    )
    assert evidence.declared is not None
    assert evidence.declared.total == 2
    assert evidence.declared.covered == 1
    assert evidence.value == 0.5
    assert JOURNEY_B in evidence.declared.uncovered
    item_b = next(i for i in evidence.items if i.journey_key == JOURNEY_B)
    assert item_b.quarantined is True
    assert item_b.covered is False
    assert item_b.status == "quarantined"
    assert any(
        board.code == "quarantined_excluded" and JOURNEY_B in board.detail for board in evidence.shortboards
    )
