"""materialize-minimum-coverage — deterministic MRC × execution join (Task 5)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from assurance_agent.artifacts.models.minimum_coverage import (
    MinimumCoverageMatrix,
    MrcFinding,
    MrcObligation,
    maps_from_advisory_mrc,
)
from assurance_agent.artifacts.models.trace import (
    TraceExecution,
    TraceProjection,
    TraceRow,
)
from assurance_agent.workflow.metrics.minimum_coverage import (
    materialize_minimum_coverage,
    obligations_from_matrix,
    shadow_compare_minimum_coverage,
)

CHANGE_ID = "CH-MRC-JOIN-001"
TS = datetime(2026, 7, 25, 12, 0, 0, tzinfo=UTC)

ARCHIVE = (
    Path(__file__).resolve().parents[4]
    / "benchmark/vue-fastapi-admin/qa/archive/RET-dept-management-20260725-124844-cursor"
)


def _row(
    case_id: str,
    *,
    status: str | None,
    case_type: str = "API",
    open_problems: tuple[str, ...] = (),
) -> TraceRow:
    latest = (
        None
        if status is None
        else TraceExecution(
            batch_id="20260725-120000",
            target="api" if case_type == "API" else "e2e",
            status=status,  # type: ignore[arg-type]
            ts=TS,
            ts_source="executed_at",
        )
    )
    return TraceRow(
        case_id=case_id,
        module="system.dept",
        case_type=case_type,  # type: ignore[arg-type]
        automation_required=True,
        assertions=("assert something",),
        covering_tests=(),
        coverage_state="covered" if status is not None else "uncovered",
        latest_execution=latest,
        freshest_pass=latest if status == "passed" else None,
        presence_in_current_batch="executed" if status is not None else "not_in_current_batch",
        atemporal_kinds_present=(),
        open_problem_ids=open_problems,
    )


def _projection(rows: tuple[TraceRow, ...]) -> TraceProjection:
    return TraceProjection(
        schema_version="1",
        change_id=CHANGE_ID,
        phase="reconciled",
        authoritative_batch_id="20260725-120000",
        sources=(),
        rows=rows,
        unmapped_tests=(),
        gaps=(),
        integrity="complete",
    )


def test_join_emits_covered_but_failing_and_not_executed() -> None:
    obligations = (
        MrcObligation(
            mrc_id="MRC-NEG-001",
            key="entities.dept.constraints.name_unique",
            category="negative",
            required=True,
            layer="api",
            case_ids=("TC_FAIL",),
        ),
        MrcObligation(
            mrc_id="MRC-API-001",
            key="create_dept",
            category="api",
            required=True,
            layer="api",
            case_ids=("TC_PASS",),
        ),
        MrcObligation(
            mrc_id="MRC-E2E-001",
            key="admin_creates_dept_and_sees_in_tree",
            category="e2e_if_enabled",
            required=True,
            layer="e2e",
            case_ids=("TC_MISSING_RUN",),
        ),
        MrcObligation(
            mrc_id="MRC-DATA-001",
            key="entities.dept.constraints.parent_child",
            category="data_integrity",
            required=True,
            layer="api",
            case_ids=(),
        ),
    )
    projection = _projection(
        (
            _row("TC_FAIL", status="failed"),
            _row("TC_PASS", status="passed"),
            _row("TC_MISSING_RUN", status=None, case_type="E2E"),
        )
    )

    result = materialize_minimum_coverage(
        change_id=CHANGE_ID,
        obligations=obligations,
        projection=projection,
    )

    by_id = {item.mrc_id: item for item in result.items}
    assert by_id["MRC-NEG-001"].status == "covered_but_failing"
    assert by_id["MRC-NEG-001"].executed_case_ids == ("TC_FAIL",)
    assert by_id["MRC-API-001"].status == "covered"
    assert by_id["MRC-API-001"].executed_case_ids == ("TC_PASS",)
    assert by_id["MRC-E2E-001"].status == "not_executed"
    assert by_id["MRC-E2E-001"].executed_case_ids == ()
    assert by_id["MRC-DATA-001"].status == "missing"
    assert result.summary.covered_but_failing == 1
    assert result.summary.not_executed == 1
    assert result.summary.missing == 1
    assert result.summary.covered == 1


def test_join_is_deterministic_for_same_inputs() -> None:
    obligations = (
        MrcObligation(
            mrc_id="MRC-API-001",
            key="create_dept",
            category="api",
            required=True,
            layer="api",
            case_ids=("TC_PASS", "TC_FAIL"),
        ),
    )
    projection = _projection((_row("TC_PASS", status="passed"), _row("TC_FAIL", status="failed")))
    a = materialize_minimum_coverage(change_id=CHANGE_ID, obligations=obligations, projection=projection)
    b = materialize_minimum_coverage(change_id=CHANGE_ID, obligations=obligations, projection=projection)
    assert a.model_dump(mode="json") == b.model_dump(mode="json")


def test_duplicate_case_id_in_projection_is_last_wins() -> None:
    obligations = (
        MrcObligation(
            mrc_id="MRC-API-001",
            key="create_dept",
            category="api",
            required=True,
            layer="api",
            case_ids=("TC_DUP",),
        ),
    )
    projection = _projection(
        (
            _row("TC_DUP", status="failed"),
            _row("TC_DUP", status="passed"),  # last-wins → covered
        )
    )
    result = materialize_minimum_coverage(change_id=CHANGE_ID, obligations=obligations, projection=projection)
    assert result.items[0].status == "covered"
    assert result.items[0].executed_case_ids == ("TC_DUP",)


def test_obligations_from_matrix_does_not_default_missing_category_to_api() -> None:
    matrix = MinimumCoverageMatrix.model_validate(
        [
            {
                "mrc_id": "missing_required_fields",
                "key": "missing_required_fields",
                "required": True,
                "covered_by_cases": ["TC_A"],
                "status": "covered",
            }
        ]
    )
    lifted = obligations_from_matrix(matrix.root)
    assert lifted.obligations == ()
    assert lifted.findings == (
        MrcFinding(
            code="mrc_category_unresolved",
            key="missing_required_fields",
            detail="matrix row lacks category and no advisory map entry",
        ),
    )


def test_obligations_from_matrix_uses_advisory_maps_for_legacy_short_keys() -> None:
    matrix = MinimumCoverageMatrix.model_validate(
        [
            {
                "mrc_id": "missing_required_fields",
                "key": "missing_required_fields",
                "required": True,
                "covered_by_cases": ["TC_A"],
                "status": "covered",
            },
            {
                "mrc_id": "admin_creates_dept_and_sees_in_tree",
                "key": "admin_creates_dept_and_sees_in_tree",
                "required": True,
                "covered_by_cases": ["TC_B"],
                "status": "covered",
            },
        ]
    )
    category_by_key, layer_by_key, mrc_id_by_key = maps_from_advisory_mrc(
        {
            "negative": ["missing_required_fields"],
            "e2e_if_enabled": ["admin_creates_dept_and_sees_in_tree"],
        }
    )
    lifted = obligations_from_matrix(
        matrix.root,
        category_by_key=category_by_key,
        layer_by_key=layer_by_key,
        mrc_id_by_key=mrc_id_by_key,
    )
    assert lifted.findings == ()
    by_key = {item.key: item for item in lifted.obligations}
    assert by_key["missing_required_fields"].category == "negative"
    assert by_key["missing_required_fields"].mrc_id == "MRC-NEGATIVE-001"
    assert by_key["admin_creates_dept_and_sees_in_tree"].category == "e2e_if_enabled"
    assert by_key["admin_creates_dept_and_sees_in_tree"].layer == "e2e"
    assert by_key["admin_creates_dept_and_sees_in_tree"].mrc_id == "MRC-E2E-001"


def _projection_from_archive_execution(archive: Path, change_id: str) -> TraceProjection:
    """Build a frozen projection from archived api/e2e result JSONs (not from MRC result)."""
    batch_dirs = sorted((archive / "execution" / "runs").iterdir())
    assert batch_dirs, "archive missing execution/runs"
    batch_id = batch_dirs[-1].name
    rows: list[TraceRow] = []
    for target, filename, case_type in (
        ("api", "api-result.json", "API"),
        ("e2e", "e2e-result.json", "E2E"),
    ):
        payload = json.loads((batch_dirs[-1] / filename).read_text(encoding="utf-8"))
        for case in payload.get("cases") or []:
            case_id = case["case_id"]
            status = case["status"]
            latest = TraceExecution(
                batch_id=batch_id,
                target=target,  # type: ignore[arg-type]
                status=status,
                ts=TS,
                ts_source="executed_at",
            )
            rows.append(
                TraceRow(
                    case_id=case_id,
                    module="system.dept",
                    case_type=case_type,  # type: ignore[arg-type]
                    automation_required=True,
                    assertions=("archive fixture",),
                    covering_tests=(),
                    coverage_state="covered",
                    latest_execution=latest,
                    freshest_pass=latest if status == "passed" else None,
                    presence_in_current_batch="executed",
                    atemporal_kinds_present=(),
                    open_problem_ids=(),
                )
            )
    return TraceProjection(
        schema_version="1",
        change_id=change_id,
        phase="reconciled",
        authoritative_batch_id=batch_id,
        sources=(),
        rows=tuple(rows),
        unmapped_tests=(),
        gaps=(),
        integrity="complete",
    )


@pytest.mark.skipif(not ARCHIVE.is_dir(), reason="dept-management archive fixture missing")
def test_shadow_compare_matches_archived_skill_result_on_same_inputs() -> None:
    """Same-input field parity: archive matrix × advisory maps × real execution.

    Does **not** read category / executed_case_ids / status from the legacy result
    when building join inputs. Legacy JSON is assertion-only.
    Intentional non-parity: ``findings`` (skill never emitted §12.12 findings).
    """
    legacy = json.loads((ARCHIVE / "report/minimum-coverage-result.json").read_text(encoding="utf-8"))
    matrix = MinimumCoverageMatrix.model_validate(
        yaml.safe_load((ARCHIVE / "trace/minimum-coverage-matrix.json").read_text(encoding="utf-8"))
    )
    advisory = json.loads((ARCHIVE / "explore/advisory.json").read_text(encoding="utf-8"))
    category_by_key, layer_by_key, mrc_id_by_key = maps_from_advisory_mrc(
        advisory.get("minimum_required_coverage")
    )
    lifted = obligations_from_matrix(
        matrix.root,
        category_by_key=category_by_key,
        layer_by_key=layer_by_key,
        mrc_id_by_key=mrc_id_by_key,
    )
    assert lifted.findings == (), lifted.findings

    projection = _projection_from_archive_execution(ARCHIVE, legacy["change_id"])
    result = materialize_minimum_coverage(
        change_id=legacy["change_id"],
        obligations=lifted.obligations,
        projection=projection,
    )
    diff = shadow_compare_minimum_coverage(result, legacy)
    assert diff == [], diff
    # Findings are a new §12.12 channel (not compared); legacy JSON has none.
    assert "findings" not in legacy
