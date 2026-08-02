"""TraceProjection fact models: frozen, forbid extras, stable gap codes."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from assurance_agent.artifacts.models.trace import (
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
