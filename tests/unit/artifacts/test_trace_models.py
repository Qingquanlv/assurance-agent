"""TraceProjection fact models: frozen, forbid extras, stable gap codes."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from assurance_agent.artifacts.models.trace import (
    TraceProblemFact,
    TraceExecution,
    TraceFailure,
    TraceGap,
    TraceGapV1,
    TraceProjection,
    TraceProjectionDocument,
    TraceProjectionV1,
    TraceProjectionV2,
    TraceRow,
    TraceSource,
    TraceTestRef,
    UnmappedTest,
    load_trace_projection_document,
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


def _execution(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "batch_id": "20260729-120000",
        "target": "api",
        "status": "passed",
        "ts": "2026-07-29T12:00:00Z",
        "ts_source": "executed_at",
    }
    base.update(overrides)
    return base


def valid_projection_v1() -> dict[str, Any]:
    return {
        "schema_version": "1",
        "change_id": "CH-1",
        "phase": "execution",
        "authoritative_batch_id": "20260729-120000",
        "sources": [
            {
                "path": "execution/execution-manifest.yaml#fold-view",
                "exists": True,
                "sha256": "abc",
            }
        ],
        "rows": [
            {
                "case_id": "TC_DEPT_API_001",
                "module": "system.dept",
                "case_type": "API",
                "automation_required": True,
                "coverage_state": "covered",
                "presence_in_current_batch": "executed",
                "covering_tests": [
                    {
                        "file": "tests/api/test_dept.py",
                        "function": "test_tc_dept_api_001__x",
                    }
                ],
                "latest_execution": _execution(),
                "atemporal_kinds_present": ["covered"],
            }
        ],
        "unmapped_tests": [
            {"file": "tests/api/test_x.py", "test_name": "test_orphan"},
        ],
        "gaps": [],
        "integrity": "complete",
    }


def valid_projection_v2() -> dict[str, Any]:
    payload = valid_projection_v1()
    payload["schema_version"] = "2"
    return payload


def mutate_trace_v2(payload: dict[str, Any], mutation: str) -> dict[str, Any]:
    mutated = deepcopy(payload)
    if mutation == "duplicate_case":
        mutated["rows"] = [*mutated["rows"], deepcopy(mutated["rows"][0])]
    elif mutation == "duplicate_source":
        mutated["sources"] = [*mutated["sources"], deepcopy(mutated["sources"][0])]
    elif mutation == "duplicate_gap":
        gap = {
            "code": "result_missing",
            "source": "execution/runs/b/api-result.json",
        }
        mutated["gaps"] = [gap, deepcopy(gap)]
    elif mutation == "execution_enrichment":
        mutated["phase"] = "execution"
        mutated["rows"][0]["failures"] = [
            {"category": "assertion_failure", "severity": "high"},
        ]
        mutated["rows"][0]["open_problem_ids"] = ["PRB-1"]
    elif mutation == "coverage_mismatch":
        mutated["rows"][0]["automation_required"] = True
        mutated["rows"][0]["coverage_state"] = "not_required"
    else:
        raise AssertionError(f"unknown mutation: {mutation}")
    return mutated


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
    assert spec.model is TraceProjectionDocument
    assert spec.compat == "versioned"
    assert match_artifact("execution/runs/b1/trace-projection.json") is None


def test_legacy_names_remain_v1_aliases() -> None:
    assert TraceProjection is TraceProjectionV1
    assert TraceGap is TraceGapV1


def test_trace_document_reads_legacy_missing_version_only() -> None:
    payload = valid_projection_v1()
    payload.pop("schema_version")
    loaded = load_trace_projection_document(payload)
    assert isinstance(loaded, TraceProjectionV1)

    for invalid in (None, "", "99"):
        payload["schema_version"] = invalid
        with pytest.raises(ValidationError):
            load_trace_projection_document(payload)


def test_trace_v1_rejects_v2_recovery_gap() -> None:
    payload = valid_projection_v1()
    payload["gaps"] = [
        {
            "code": "project_sync_pending",
            "source": "inspect/issue-reconcile-status.json",
        }
    ]
    with pytest.raises(ValidationError):
        TraceProjectionV1.model_validate(payload)


@pytest.mark.parametrize(
    "mutation",
    [
        "duplicate_case",
        "duplicate_source",
        "duplicate_gap",
        "execution_enrichment",
        "coverage_mismatch",
    ],
)
def test_trace_v2_rejects_semantic_mutations(mutation: str) -> None:
    payload = mutate_trace_v2(valid_projection_v2(), mutation)
    with pytest.raises(ValidationError):
        TraceProjectionV2.model_validate(payload)


def test_trace_v1_and_v2_round_trip() -> None:
    v1 = TraceProjectionV1.model_validate(valid_projection_v1())
    assert TraceProjectionV1.model_validate_json(v1.model_dump_json()) == v1

    v2_payload = valid_projection_v2()
    v2_payload["gaps"] = [
        {
            "code": "project_sync_pending",
            "source": "inspect/issue-reconcile-status.json",
            "target": "api",
        }
    ]
    v2_payload["integrity"] = "incomplete"
    v2 = TraceProjectionV2.model_validate(v2_payload)
    assert TraceProjectionV2.model_validate_json(v2.model_dump_json()) == v2
    loaded = load_trace_projection_document(v2.model_dump(mode="json"))
    assert isinstance(loaded, TraceProjectionV2)
    assert loaded == v2


def test_trace_document_rejects_unknown_version() -> None:
    payload = valid_projection_v1()
    payload["schema_version"] = "3"
    with pytest.raises(ValidationError):
        load_trace_projection_document(payload)


def test_trace_v2_rejects_execution_target_case_type_mismatch() -> None:
    payload = valid_projection_v2()
    payload["rows"][0]["latest_execution"] = _execution(target="e2e")
    with pytest.raises(ValidationError):
        TraceProjectionV2.model_validate(payload)


def test_trace_v2_rejects_unknown_gap_target() -> None:
    payload = valid_projection_v2()
    payload["gaps"] = [
        {
            "code": "result_missing",
            "source": "execution/runs/b/api-result.json",
            "target": "backend",
        }
    ]
    with pytest.raises(ValidationError):
        TraceProjectionV2.model_validate(payload)


def test_trace_v2_accepts_four_layer_gap_targets() -> None:
    for target in ("api", "e2e", "fuzz", "performance", None):
        payload = valid_projection_v2()
        payload["gaps"] = [
            {
                "code": "result_missing",
                "source": "execution/runs/b/api-result.json",
                "target": target,
            }
        ]
        payload["integrity"] = "incomplete"
        TraceProjectionV2.model_validate(payload)

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
