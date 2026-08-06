"""inspect/coverage-gaps.json — typed dual-source gap signals (Phase 2)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from assurance_agent.artifacts.models.coverage_gaps import (
    COVERAGE_GAPS_REL,
    CoverageGap,
    CoverageGapKind,
    CoverageGapLayer,
    CoverageGapLocator,
    CoverageGapsDocument,
)
from assurance_agent.artifacts.registry import match_artifact


def _gap(**overrides: object) -> CoverageGap:
    payload: dict[str, object] = {
        "kind": "uncovered_required_case",
        "locator": {"case_id": "TC_API_001"},
        "layer": "execution",
        "batch_id": "20260805-120000",
        "evidence_refs": ("sha256:abc",),
    }
    payload.update(overrides)
    return CoverageGap.model_validate(payload)


def test_unknown_kind_is_fail_closed() -> None:
    with pytest.raises(ValidationError):
        _gap(kind="invented_gap_kind")


def test_closed_kind_vocabulary() -> None:
    kinds = set(CoverageGapKind.__args__)  # type: ignore[attr-defined]
    assert kinds == {
        "uncovered_required_case",
        "stale_required_case",
        "constraint_without_property",
        "matrix_cell_unasserted",
        "unmapped_test_cluster",
    }


def test_layer_vocabulary_is_execution_or_declaration() -> None:
    layers = set(CoverageGapLayer.__args__)  # type: ignore[attr-defined]
    assert layers == {"execution", "declaration"}


def test_document_orders_gaps_deterministically() -> None:
    doc = CoverageGapsDocument(
        schema_version="1",
        change_id="CH-GAP-001",
        batch_id="20260805-120000",
        projection_digest="sha256:deadbeef",
        gaps=(
            _gap(kind="unmapped_test_cluster", locator={"cluster_key": "tests/api/z.py"}),
            _gap(kind="uncovered_required_case", locator={"case_id": "TC_B"}),
            _gap(kind="uncovered_required_case", locator={"case_id": "TC_A"}),
        ),
    )
    assert [g.kind for g in doc.gaps] == [
        "uncovered_required_case",
        "uncovered_required_case",
        "unmapped_test_cluster",
    ]
    assert [g.locator.case_id for g in doc.gaps[:2]] == ["TC_A", "TC_B"]


def test_locator_requires_at_least_one_key() -> None:
    with pytest.raises(ValidationError, match="locator"):
        CoverageGapLocator()


def test_registry_matches_inspect_coverage_gaps_must_compat() -> None:
    spec = match_artifact(COVERAGE_GAPS_REL)
    assert spec is not None
    assert spec.artifact_type == "coverage_gaps"
    assert spec.compat == "must_compat"
    assert spec.model is CoverageGapsDocument
