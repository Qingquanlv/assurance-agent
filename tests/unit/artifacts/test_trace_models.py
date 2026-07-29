"""TraceProjection fact models: frozen, forbid extras, stable gap codes."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from assurance_agent.artifacts.models.trace import (
    TraceExecution,
    TraceFailure,
    TraceGap,
    TraceProjection,
    TraceRow,
    TraceSource,
    TraceTestRef,
    UnmappedTest,
)
from assurance_agent.artifacts.registry import match_artifact


def _row(**overrides: object) -> TraceRow:
    base: dict[str, object] = {
        "case_id": "TC_DEPT_API_001",
        "module": "system.dept",
        "case_type": "API",
        "automation_required": True,
        "coverage_state": "covered",
        "presence_in_current_batch": "executed",
    }
    base.update(overrides)
    return TraceRow.model_validate(base)


def test_projection_round_trip() -> None:
    ts = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)
    doc = TraceProjection(
        change_id="CH-1",
        phase="execution",
        authoritative_batch_id="20260729-120000",
        sources=(TraceSource(path="execution/execution-manifest.yaml#fold-view", exists=True, sha256="abc"),),
        rows=(
            _row(
                covering_tests=(
                    TraceTestRef(file="tests/api/test_dept.py", function="test_tc_dept_api_001__x"),
                ),
                latest_execution=TraceExecution(
                    batch_id="20260729-120000",
                    target="api",
                    status="passed",
                    ts=ts,
                    ts_source="executed_at",
                ),
                atemporal_kinds_present=("covered",),
            ),
        ),
        unmapped_tests=(UnmappedTest(file="tests/api/test_x.py", test_name="test_orphan"),),
        gaps=(),
        integrity="complete",
    )
    restored = TraceProjection.model_validate_json(doc.model_dump_json())
    assert restored == doc


def test_unknown_gap_code_is_rejected() -> None:
    with pytest.raises(ValidationError):
        TraceGap(code="not_a_real_gap", source="x")  # type: ignore[arg-type]


def test_unknown_extra_field_is_rejected() -> None:
    with pytest.raises(ValidationError):
        TraceProjection.model_validate(
            {
                "schema_version": "1",
                "change_id": "CH-1",
                "phase": "execution",
                "authoritative_batch_id": "b",
                "integrity": "complete",
                "surprise": True,
            }
        )


def test_failures_is_a_tuple_not_a_singular_field() -> None:
    row = _row(
        failures=(
            TraceFailure(category="assertion_failure", severity="high"),
            TraceFailure(category="unknown", severity="low"),
        )
    )
    assert len(row.failures) == 2
    dumped = row.model_dump()
    assert "failure" not in dumped
    assert dumped["failures"][0]["category"] == "assertion_failure"


def test_ts_source_enum() -> None:
    ts = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)
    executed = TraceExecution(batch_id="b", target="api", status="passed", ts=ts, ts_source="executed_at")
    legacy = TraceExecution(
        batch_id="b", target="api", status="passed", ts=ts, ts_source="batch_id_legacy_utc"
    )
    assert executed.ts_source == "executed_at"
    assert legacy.ts_source == "batch_id_legacy_utc"
    with pytest.raises(ValidationError):
        TraceExecution(batch_id="b", target="api", status="passed", ts=ts, ts_source="mtime")  # type: ignore[arg-type]


def test_registry_binds_inspect_trace_projection_only() -> None:
    spec = match_artifact("inspect/trace-projection.json")
    assert spec is not None
    assert spec.artifact_type == "trace_projection"
    assert spec.model is TraceProjection
    assert match_artifact("execution/runs/b1/trace-projection.json") is None
