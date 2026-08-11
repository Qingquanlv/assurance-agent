"""CLayerMetricsDocument — report-only, outside MetricKey."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from assurance_agent.artifacts.models.c_layer import (
    C_LAYER_METRICS_REL,
    C_LAYER_VECTOR_KEYS,
    CLayerMetricEntry,
    CLayerMetricsDocument,
)
from assurance_agent.artifacts.models.metrics import METRIC_KEYS
from assurance_agent.artifacts.registry import match_artifact


def _entry(**overrides: object) -> CLayerMetricEntry:
    base: dict[str, object] = {
        "status": "evaluated",
        "numerator": 1,
        "denominator": 2,
        "rate": 0.5,
        "evidence_digests": ("sha256:" + ("a" * 64),),
    }
    base.update(overrides)
    return CLayerMetricEntry.model_validate(base)


def _doc(**overrides: object) -> CLayerMetricsDocument:
    pending = CLayerMetricEntry(status="not_evaluated")
    base: dict[str, object] = {
        "schema_version": "1",
        "change_id": "CH-C-001",
        "cadence": "report",
        "computed_at": datetime(2026, 8, 5, 12, 0, tzinfo=UTC),
        "escape_rate": pending,
        "counterexample_promotion_rate": pending,
        "coverage_gap_closure_rate": pending,
        "seed_replay_stability": pending,
    }
    base.update(overrides)
    return CLayerMetricsDocument.model_validate(base)


def test_c_layer_path_and_vector_keys_are_closed() -> None:
    assert C_LAYER_METRICS_REL == "inspect/metrics-c-layer.json"
    assert C_LAYER_VECTOR_KEYS == (
        "escape_rate",
        "counterexample_promotion_rate",
        "coverage_gap_closure_rate",
        "seed_replay_stability",
    )
    # Option A: never pollute MetricKey / floors.
    assert set(C_LAYER_VECTOR_KEYS).isdisjoint(set(METRIC_KEYS))


def test_registry_matches_c_layer_metrics_must_compat() -> None:
    spec = match_artifact(C_LAYER_METRICS_REL)
    assert spec is not None
    assert spec.artifact_type == "c_layer_metrics"
    assert spec.compat == "must_compat"
    assert spec.model is CLayerMetricsDocument


def test_evaluated_requires_positive_denom_and_rate() -> None:
    with pytest.raises(ValidationError):
        _entry(denominator=0, rate=None, status="evaluated")
    with pytest.raises(ValidationError):
        _entry(rate=0.6)
    with pytest.raises(ValidationError):
        _entry(numerator=3, denominator=2, rate=1.5)


def test_not_evaluated_forbids_rate_and_invented_nonzero_denom() -> None:
    assert _entry(status="not_evaluated", numerator=None, denominator=None, rate=None)
    assert _entry(status="not_evaluated", numerator=0, denominator=0, rate=None)
    with pytest.raises(ValidationError):
        _entry(status="not_evaluated", numerator=0, denominator=0, rate=0.0)
    with pytest.raises(ValidationError):
        _entry(status="not_evaluated", numerator=1, denominator=2, rate=None)
    with pytest.raises(ValidationError):
        _entry(status="not_evaluated", numerator=0, denominator=None, rate=None)


def test_document_holds_four_independent_fields() -> None:
    doc = _doc(
        escape_rate=_entry(numerator=1, denominator=4, rate=0.25),
        counterexample_promotion_rate=_entry(numerator=2, denominator=5, rate=0.4),
        coverage_gap_closure_rate=_entry(numerator=3, denominator=3, rate=1.0),
        seed_replay_stability=_entry(numerator=1, denominator=1, rate=1.0),
    )
    assert doc.cadence == "report"
    assert doc.vector("escape_rate").rate == 0.25
    assert doc.vector("counterexample_promotion_rate").rate == 0.4
    assert doc.vector("coverage_gap_closure_rate").rate == 1.0
    assert doc.vector("seed_replay_stability").rate == 1.0
    # No confidence alias on the wire.
    dumped = doc.model_dump(mode="json")
    assert "confidence" not in dumped
    assert "confidence" not in str(dumped)
