from datetime import datetime, timezone
from typing import Any

import pytest
from pydantic import ValidationError

from assurance_agent.artifacts.models.trace import (
    TraceExecution,
    TraceFailure,
    TraceGap,
    TraceProblemFact,
    TraceProjection,
    TraceRow,
    TraceSource,
    TraceTestRef,
    UnmappedTest,
)
from assurance_agent.artifacts.registry import match_artifact


def _row(**overrides: Any) -> TraceRow:
    base: dict[str, Any] = dict(
        case_id="TC_APIS_API_001",
        module="system.api",
        case_type="API",
        automation_required=True,
        assertions=("接口返回 HTTP 200",),
        covering_tests=(
            TraceTestRef(file="tests/api/test_api.py", test_name="test_TC_APIS_API_001__create"),
        ),
        coverage_state="covered",
        latest_execution=TraceExecution(
            batch_id="20260804-100000",
            target="api",
            status="passed",
            ts=datetime(2026, 8, 4, 10, 0, 0, tzinfo=timezone.utc),
            ts_source="executed_at",
        ),
        freshest_pass=None,
        presence_in_current_batch="executed",
        atemporal_kinds_present=("covered",),
        failures=(),
        open_problem_ids=(),
    )
    base.update(overrides)
    return TraceRow(**base)


def _projection(**overrides: Any) -> TraceProjection:
    base: dict[str, Any] = dict(
        schema_version="1",
        change_id="RET-api-management",
        phase="execution",
        authoritative_batch_id="20260804-100000",
        sources=(TraceSource(path="execution/execution-manifest.yaml", exists=True, sha256="a" * 64),),
        rows=(_row(),),
        unmapped_tests=(),
        gaps=(),
        integrity="complete",
    )
    base.update(overrides)
    return TraceProjection(**base)


def test_valid_trace_projection_round_trips_through_json() -> None:
    projection = _projection()
    dumped = projection.model_dump(mode="json")
    restored = TraceProjection.model_validate(dumped)
    assert restored == projection


def test_trace_gap_rejects_unknown_code() -> None:
    with pytest.raises(ValidationError):
        TraceGap(code="not_a_real_code", source="execution/api-result.json")  # type: ignore[arg-type]


def test_trace_gap_accepts_all_twelve_documented_codes() -> None:
    codes = (
        "result_missing",
        "result_corrupt",
        "batch_id_unparseable",
        "manifest_missing",
        "case_unreadable",
        "failure_analysis_missing",
        "issues_snapshot_missing",
        "problems_snapshot_missing",
        "mapped_test_missing_from_tree",
        "tests_tree_digest_mismatch",
        "result_identity_mismatch",
        "problem_alias_invalid",
    )
    for code in codes:
        gap = TraceGap(code=code, source="some/source")
        assert gap.code == code


def test_trace_row_rejects_unknown_extra_field() -> None:
    with pytest.raises(ValidationError):
        _row(unexpected_field="oops")


def test_trace_projection_rejects_unknown_extra_field() -> None:
    with pytest.raises(ValidationError):
        _projection(unexpected_field="oops")


def test_trace_row_failures_is_a_tuple_and_preserves_document_order() -> None:
    failures = (
        TraceFailure(category="assertion_failure", severity="high"),
        TraceFailure(category="classification_unavailable", severity="low"),
    )
    row = _row(failures=failures, coverage_state="uncovered")
    assert row.failures == failures
    assert isinstance(row.failures, tuple)


def _problem_fact(**overrides: Any) -> TraceProblemFact:
    base: dict[str, Any] = dict(
        problem_id="PB-100",
        source_problem_ids=("PB-001", "PB-100"),
        fingerprint="sha256:" + "a" * 64,
        status="triaged",
        classification="product_bug",
        open_product_bug=True,
    )
    base.update(overrides)
    return TraceProblemFact(**base)


def test_trace_problem_fact_records_the_canonical_problem_and_its_aliases() -> None:
    fact = _problem_fact()
    assert fact.problem_id == "PB-100"
    assert fact.source_problem_ids == ("PB-001", "PB-100")
    assert isinstance(fact.source_problem_ids, tuple)
    assert fact.open_product_bug is True


def test_trace_problem_fact_records_closed_and_non_product_problems() -> None:
    """§9.4/§9.5 facts are recorded, not filtered: only routing filters."""
    closed = _problem_fact(status="resolved", open_product_bug=False)
    non_product = _problem_fact(classification="test_bug", open_product_bug=False)
    assert (closed.status, closed.open_product_bug) == ("resolved", False)
    assert (non_product.classification, non_product.open_product_bug) == ("test_bug", False)


def test_trace_problem_fact_is_frozen_and_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        _problem_fact(unexpected_field="oops")
    fact = _problem_fact()
    with pytest.raises(ValidationError):
        fact.problem_id = "PB-999"  # type: ignore[misc]


def test_trace_row_problem_facts_defaults_to_empty_for_compatibility() -> None:
    """A projection written before this field validates unchanged."""
    row = _row()
    assert row.problem_facts == ()
    legacy = row.model_dump(mode="json")
    del legacy["problem_facts"]
    assert TraceRow.model_validate(legacy).problem_facts == ()


def test_trace_row_carries_problem_facts_through_a_json_round_trip() -> None:
    facts = (_problem_fact(), _problem_fact(problem_id="PB-200", source_problem_ids=("PB-200",)))
    projection = _projection(rows=(_row(problem_facts=facts, open_problem_ids=("PB-100", "PB-200")),))

    restored = TraceProjection.model_validate(projection.model_dump(mode="json"))

    assert restored == projection
    assert restored.rows[0].problem_facts == facts


def test_trace_execution_ts_source_enum_rejects_unknown_value() -> None:
    with pytest.raises(ValidationError):
        TraceExecution(
            batch_id="20260804-100000",
            target="api",
            status="passed",
            ts=datetime(2026, 8, 4, 10, 0, 0, tzinfo=timezone.utc),
            ts_source="wall_clock_now",  # type: ignore[arg-type]
        )


def test_trace_execution_ts_source_accepts_both_documented_values() -> None:
    for source in ("executed_at", "batch_id_legacy_utc"):
        execution = TraceExecution(
            batch_id="20260804-100000",
            target="api",
            status="passed",
            ts=datetime(2026, 8, 4, 10, 0, 0, tzinfo=timezone.utc),
            ts_source=source,
        )
        assert execution.ts_source == source


def test_all_models_are_frozen() -> None:
    projection = _projection()
    with pytest.raises(ValidationError):
        projection.change_id = "OTHER"  # type: ignore[misc]


def test_unmapped_test_and_trace_source_shapes() -> None:
    unmapped = UnmappedTest(file="tests/api/test_x.py", test_name="test_unrelated")
    assert unmapped.file == "tests/api/test_x.py"
    source = TraceSource(path="inspect/failure-analysis.json", exists=False, sha256=None)
    assert source.exists is False
    assert source.sha256 is None


def test_trace_projection_matches_registered_inspect_path() -> None:
    spec = match_artifact("inspect/trace-projection.json")
    assert spec is not None
    assert spec.artifact_type == "trace_projection"
    assert spec.model is TraceProjection
    assert spec.compat == "must_compat"


def test_trace_projection_pattern_does_not_match_other_inspect_paths() -> None:
    assert match_artifact("inspect/failure-analysis.json") is not None
    spec = match_artifact("inspect/failure-analysis.json")
    assert spec is not None and spec.artifact_type != "trace_projection"
