"""compute-journey-coverage — MRC journey × case × execution (Task 6 / §5-A4)."""

from __future__ import annotations

from datetime import UTC, datetime

from assurance_agent.artifacts.models.minimum_coverage import MrcObligation
from assurance_agent.artifacts.models.trace import (
    TraceExecution,
    TraceProjection,
    TraceRow,
    TraceTestRef,
)
from assurance_agent.workflow.metrics.journey_coverage import compute_journey_coverage

CHANGE_ID = "CH-A4-001"
BATCH = "20260805-100000"
STALE_BATCH = "20260804-090000"
TS = datetime(2026, 8, 5, 10, 0, 0, tzinfo=UTC)
JOURNEY = "admin_creates_dept_and_sees_in_tree"

STRONG_E2E = """
async def test_admin_creates_dept():
    await page.goto("/dept")
    await page.get_by_role("button", name="Create").click()
    await expect(page.get_by_text("Dept A")).to_have_count(1)
"""

WEAK_E2E = """
async def test_admin_creates_dept():
    await page.goto("/dept")
    await expect(page.get_by_role("heading")).to_be_visible()
"""


def _row(
    case_id: str,
    *,
    status: str | None,
    batch_id: str = BATCH,
    presence: str | None = None,
) -> TraceRow:
    latest = (
        None
        if status is None
        else TraceExecution(
            batch_id=batch_id,
            target="e2e",
            status=status,  # type: ignore[arg-type]
            ts=TS,
            ts_source="executed_at",
        )
    )
    if presence is None:
        presence = "executed" if status is not None and batch_id == BATCH else "not_in_current_batch"
    return TraceRow(
        case_id=case_id,
        module="system.dept",
        case_type="E2E",
        automation_required=True,
        assertions=("ok",),
        covering_tests=(TraceTestRef(file="tests/e2e/test_dept.py", test_name="test_admin_creates_dept"),),
        coverage_state="covered" if status is not None else "uncovered",
        latest_execution=latest,
        freshest_pass=latest if status == "passed" else None,
        presence_in_current_batch=presence,  # type: ignore[arg-type]
        atemporal_kinds_present=(),
        open_problem_ids=(),
    )


def _projection(rows: tuple[TraceRow, ...]) -> TraceProjection:
    return TraceProjection(
        schema_version="1",
        change_id=CHANGE_ID,
        phase="reconciled",
        authoritative_batch_id=BATCH,
        sources=(),
        rows=rows,
        unmapped_tests=(),
        gaps=(),
        integrity="complete",
    )


def _obligation(**overrides: object) -> MrcObligation:
    payload: dict[str, object] = {
        "mrc_id": "MRC-E2E-001",
        "key": JOURNEY,
        "category": "e2e_if_enabled",
        "required": True,
        "layer": "e2e",
        "case_ids": ("TC_E2E_001",),
    }
    payload.update(overrides)
    return MrcObligation.model_validate(payload)


def test_stale_latest_execution_from_other_batch_is_not_executed() -> None:
    """Freshness must not treat a prior-batch latest_execution as current."""
    evidence = compute_journey_coverage(
        change_id=CHANGE_ID,
        batch_id=BATCH,
        obligations=(_obligation(),),
        projection=_projection(
            (
                _row(
                    "TC_E2E_001",
                    status="passed",
                    batch_id=STALE_BATCH,
                    presence="not_in_current_batch",
                ),
            )
        ),
        quarantine=(),
        test_sources={"tests/e2e/test_dept.py": STRONG_E2E},
        case_functions={"TC_E2E_001": ("tests/e2e/test_dept.py", "test_admin_creates_dept")},
    )
    assert evidence.items[0].status == "not_executed"
    assert evidence.items[0].executed_case_ids == ()
    assert evidence.items[0].covered is False
    assert evidence.value == 0.0


def test_presence_executed_counts_even_when_latest_batch_id_differs() -> None:
    evidence = compute_journey_coverage(
        change_id=CHANGE_ID,
        batch_id=BATCH,
        obligations=(_obligation(),),
        projection=_projection(
            (
                _row(
                    "TC_E2E_001",
                    status="passed",
                    batch_id=STALE_BATCH,
                    presence="executed",
                ),
            )
        ),
        quarantine=(),
        test_sources={"tests/e2e/test_dept.py": STRONG_E2E},
        case_functions={"TC_E2E_001": ("tests/e2e/test_dept.py", "test_admin_creates_dept")},
    )
    assert evidence.items[0].status == "covered"
    assert evidence.value == 1.0


def test_missing_oracle_sources_fail_closed_not_covered() -> None:
    """Production must never treat omitted B2 sources as a strong oracle."""
    evidence = compute_journey_coverage(
        change_id=CHANGE_ID,
        batch_id=BATCH,
        obligations=(_obligation(),),
        projection=_projection((_row("TC_E2E_001", status="passed"),)),
        quarantine=(),
    )
    assert evidence.items[0].covered is False
    assert evidence.items[0].status == "oracle_unavailable"
    assert evidence.value == 0.0


def test_skipped_by_scope_journey_is_never_covered() -> None:
    evidence = compute_journey_coverage(
        change_id=CHANGE_ID,
        batch_id=BATCH,
        obligations=(
            _obligation(
                skipped_by_scope=True,
                skip_reason="frontend journey out of this PR scope",
            ),
        ),
        projection=_projection((_row("TC_E2E_001", status="passed"),)),
        quarantine=(),
        test_sources={"tests/e2e/test_dept.py": STRONG_E2E},
        case_functions={"TC_E2E_001": ("tests/e2e/test_dept.py", "test_admin_creates_dept")},
    )
    assert evidence.items[0].status == "skipped_by_scope"
    assert evidence.items[0].covered is False
    assert evidence.value == 0.0


def test_quarantine_interface_accepts_empty_and_excludes_named_journeys() -> None:
    empty = compute_journey_coverage(
        change_id=CHANGE_ID,
        batch_id=BATCH,
        obligations=(_obligation(),),
        projection=_projection((_row("TC_E2E_001", status="passed"),)),
        quarantine=(),
        test_sources={"tests/e2e/test_dept.py": STRONG_E2E},
        case_functions={"TC_E2E_001": ("tests/e2e/test_dept.py", "test_admin_creates_dept")},
    )
    assert empty.items[0].quarantined is False
    assert empty.items[0].covered is True

    blocked = compute_journey_coverage(
        change_id=CHANGE_ID,
        batch_id=BATCH,
        obligations=(_obligation(),),
        projection=_projection((_row("TC_E2E_001", status="passed"),)),
        quarantine=(JOURNEY,),
        test_sources={"tests/e2e/test_dept.py": STRONG_E2E},
        case_functions={"TC_E2E_001": ("tests/e2e/test_dept.py", "test_admin_creates_dept")},
    )
    assert blocked.items[0].status == "quarantined"
    assert blocked.items[0].covered is False
    assert blocked.value == 0.0


def test_weak_e2e_oracle_does_not_cover_when_sources_provided() -> None:
    evidence = compute_journey_coverage(
        change_id=CHANGE_ID,
        batch_id=BATCH,
        obligations=(_obligation(),),
        projection=_projection((_row("TC_E2E_001", status="passed"),)),
        quarantine=(),
        test_sources={"tests/e2e/test_dept.py": WEAK_E2E},
        case_functions={"TC_E2E_001": ("tests/e2e/test_dept.py", "test_admin_creates_dept")},
    )
    assert evidence.items[0].status == "weak_oracle"
    assert evidence.value == 0.0


def test_strong_e2e_oracle_covers_when_sources_provided() -> None:
    evidence = compute_journey_coverage(
        change_id=CHANGE_ID,
        batch_id=BATCH,
        obligations=(_obligation(),),
        projection=_projection((_row("TC_E2E_001", status="passed"),)),
        quarantine=(),
        test_sources={"tests/e2e/test_dept.py": STRONG_E2E},
        case_functions={"TC_E2E_001": ("tests/e2e/test_dept.py", "test_admin_creates_dept")},
    )
    assert evidence.items[0].status == "covered"
    assert evidence.value == 1.0
