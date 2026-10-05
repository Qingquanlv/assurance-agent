from __future__ import annotations


import pytest
from pydantic import ValidationError


from assurance_quality.contracts.trace import (
    TraceProjectionDocument,
    TraceProjectionV2,
    TraceTestRef,
)
from assurance_quality.operations.trace import (
    TraceCaseInput,
    TraceOperationInput,
    project_trace,
)
from quality_fixtures import (  # pyright: ignore[reportMissingImports]
    BATCH_ID,
    CHANGE_ID,
    LEAF,
    catalog_leafs,
)


def _trace_document(*, schema_version: str | None = "2") -> dict[str, object]:
    payload: dict[str, object] = {
        "change_id": CHANGE_ID,
        "phase": "execution",
        "authoritative_batch_id": BATCH_ID,
        "sources": [],
        "rows": [],
        "unmapped_tests": [],
        "gaps": [],
        "integrity": "complete",
    }
    if schema_version is not None:
        payload["schema_version"] = schema_version
    return payload


def trace_v1_document() -> dict[str, object]:
    return _trace_document(schema_version="1")


def trace_without_version() -> dict[str, object]:
    return _trace_document(schema_version=None)


@pytest.mark.parametrize("document", [trace_v1_document(), trace_without_version()])
def test_trace_accepts_only_explicit_v2(document: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        TraceProjectionDocument.model_validate(document)


def test_trace_test_ref_requires_test_name() -> None:
    with pytest.raises(ValidationError):
        TraceTestRef.model_validate({"file": "tests/api/test_menu.py", "function": "test_create"})


def test_trace_rejects_legacy_integrity_and_timestamp_source() -> None:
    leafs = {"capability_leafs": frozenset(catalog_leafs())}
    degraded = _trace_document()
    degraded["integrity"] = "degraded"
    with pytest.raises(ValidationError):
        TraceProjectionDocument.model_validate(degraded, context=leafs)
    execution = {
        "batch_id": BATCH_ID,
        "target": "api",
        "status": "passed",
        "ts": "2026-08-22T00:00:00Z",
        "ts_source": "batch_id_legacy_utc",
    }
    with pytest.raises(ValidationError):
        TraceProjectionV2.model_validate(
            {
                **_trace_document(),
                "rows": [
                    {
                        "case_id": "TC_A",
                        "module": "menus",
                        "case_type": "API",
                        "automation_required": True,
                        "coverage_state": "covered",
                        "presence_in_current_batch": "executed",
                        "latest_execution": execution,
                    }
                ],
            },
            context=leafs,
        )


def test_trace_without_execution_evidence_does_not_copy_allowlist_onto_every_case() -> None:
    payload = TraceOperationInput.model_validate(
        {
            "change_id": CHANGE_ID,
            "batch_id": BATCH_ID,
            "closed_mapping": ["tests/a.py", "tests/b.py"],
            "observed": ["tests/a.py", "tests/b.py"],
            "capability_leafs": catalog_leafs(),
            "case_ids": ["TC_A", "TC_B"],
            "cases": [
                {
                    "case_id": "TC_A",
                    "module": "menus",
                    "case_type": "API",
                    "automation_required": True,
                    "capability": LEAF,
                },
                {
                    "case_id": "TC_B",
                    "module": "menus",
                    "case_type": "API",
                    "automation_required": True,
                    "capability": LEAF,
                },
            ],
        }
    )
    projection = project_trace(payload)
    assert [tuple(ref.file for ref in row.covering_tests) for row in projection.rows] == [(), ()]
    assert {row.coverage_state for row in projection.rows} == {"uncovered"}

    mapped = project_trace(
        payload.model_copy(
            update={
                "case_covering": {
                    "TC_A": ("tests/a.py",),
                    "TC_B": ("tests/b.py",),
                }
            }
        )
    )
    assert [tuple(ref.file for ref in row.covering_tests) for row in mapped.rows] == [(), ()]


def test_real_unexecuted_case_never_becomes_passing_trace() -> None:
    payload = TraceOperationInput(
        change_id="CH-1",
        batch_id="B-1",
        closed_mapping=(),
        cases=(TraceCaseInput(case_id="TC_A", capability="entities.item.create"),),
        capability_leafs=("entities.item.create",),
    )
    projection = project_trace(payload)
    assert [row.case_id for row in projection.rows] == ["TC_A"]
    assert "TC_UNBOUND" not in {row.case_id for row in projection.rows}
    assert projection.rows[0].latest_execution is None
    assert projection.rows[0].freshest_pass is None


def test_empty_case_inventory_does_not_synthesize_an_unbound_case() -> None:
    projection = project_trace(
        TraceOperationInput(
            change_id="CH-1",
            batch_id="B-1",
            closed_mapping=(),
            capability_leafs=("entities.item.create",),
        )
    )
    assert projection.rows == ()
