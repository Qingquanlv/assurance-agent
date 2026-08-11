"""M2 Task 6: pure nightly metrics aggregation (evidence/metrics.py)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from assurance_agent.artifacts.canonical import canonical_json_bytes
from assurance_agent.artifacts.models.metrics import (
    MetricCollectionGap,
    MetricScope,
    MetricShortboard,
    MetricsDocument,
)
from assurance_agent.artifacts.models.policy import MetricFloor
from assurance_agent.artifacts.models.pr_metric_evidence import (
    AdversarialYieldEvidence,
    AssertionStrengthEvidence,
    AssertionStrengthSurfaceSlice,
    BaselineDriftEvidence,
    BaselineDriftScenario,
    MutationEvidence,
)
from assurance_agent.evidence.metrics import DEFAULT_NIGHTLY_KEYS, aggregate_nightly_metrics
from assurance_agent.evidence.risk_tier import resolve_risk_tier

CHANGE_ID = "CH-NIGHTLY-AGG-001"
DIGEST = "b" * 64
COMPUTED_AT = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
BATCH_ID = "nightly"


def _mutation(**overrides: Any) -> MutationEvidence:
    payload: dict[str, Any] = {
        "schema_version": "1",
        "change_id": CHANGE_ID,
        "batch_id": BATCH_ID,
        "status": "evaluated",
        "value": 0.75,
        "killed": 3,
        "survived": 1,
        "equivalent": 0,
        "tested": 4,
        "selected": 4,
        "budget_seconds": 300,
        "elapsed_seconds": 12.0,
        "survivors": (),
        "collection_gaps": (),
        "shortboards": (),
    }
    payload.update(overrides)
    return MutationEvidence.model_validate(payload)


def _assertion(**overrides: Any) -> AssertionStrengthEvidence:
    api = AssertionStrengthSurfaceSlice(
        layer="api",
        declared=MetricScope.of(total=4, covered=3),
        value=0.75,
        strong=3,
        weak=1,
    )
    e2e = AssertionStrengthSurfaceSlice(
        layer="e2e",
        declared=MetricScope.of(total=4, covered=4),
        value=1.0,
        strong=4,
        weak=0,
    )
    declared = MetricScope.of(total=8, covered=7)
    payload: dict[str, Any] = {
        "schema_version": "1",
        "change_id": CHANGE_ID,
        "batch_id": BATCH_ID,
        "status": "evaluated",
        "value": 7 / 8,
        "declared": declared,
        "surfaces": (api, e2e),
        "weak_assertions": (),
        "collection_gaps": (),
        "shortboards": (),
    }
    payload.update(overrides)
    return AssertionStrengthEvidence.model_validate(payload)


def _baseline(**overrides: Any) -> BaselineDriftEvidence:
    scenario = BaselineDriftScenario(
        capability="login",
        endpoint="/api/login",
        current_p95_ms=110.0,
        baseline_p95_ms=100.0,
        current_error_rate=0.01,
        baseline_error_rate=0.01,
        drift=0.1,
        sample_count=5,
    )
    payload: dict[str, Any] = {
        "schema_version": "1",
        "change_id": CHANGE_ID,
        "batch_id": BATCH_ID,
        "status": "evaluated",
        "value": 0.1,
        "drift_band": 0.2,
        "window": 5,
        "scenarios": (scenario,),
        "collection_gaps": (),
        "shortboards": (),
    }
    payload.update(overrides)
    return BaselineDriftEvidence.model_validate(payload)


def _yield(**overrides: Any) -> AdversarialYieldEvidence:
    payload: dict[str, Any] = {
        "schema_version": "1",
        "change_id": CHANGE_ID,
        "batch_id": BATCH_ID,
        "property": "api",
        "layer": "api",
        "sample_count": 3,
        "counterexample_ids": ("CE-1", "CE-2"),
        "unclosed_count": 1,
        "seed": 7,
        "status": "evaluated",
        "value": 2.0,
        "collection_gaps": (),
        "shortboards": (),
        "source": {},
    }
    payload.update(overrides)
    return AdversarialYieldEvidence.model_validate(payload)


def _aggregate(**overrides: Any) -> MetricsDocument:
    floors = overrides.pop(
        "floors",
        {
            "assertion_strength": MetricFloor(target="value", min=1.0),
            "baseline_drift": MetricFloor(target="value", min=0.2),
        },
    )
    payload: dict[str, Any] = {
        "change_id": CHANGE_ID,
        "computed_at": COMPUTED_AT,
        "policy_digest": DIGEST,
        "risk": resolve_risk_tier(()),
        "mutation": _mutation(),
        "assertion_strength": _assertion(),
        "baseline_drift": _baseline(),
        "adversarial_yield": None,
        "floors": floors,
    }
    payload.update(overrides)
    return aggregate_nightly_metrics(**payload)


def test_aggregate_folds_evaluated_nightly_evidence() -> None:
    doc = _aggregate()
    assert doc.cadence == "nightly"
    assert doc.change_id == CHANGE_ID
    assert doc.metrics["mutation_score"].status == "evaluated"
    assert doc.metrics["mutation_score"].value == pytest.approx(0.75)
    assert doc.metrics["mutation_score"].evidence == "mutation.json"
    assert doc.metrics["assertion_strength"].status == "evaluated"
    assert doc.metrics["assertion_strength"].value == pytest.approx(7 / 8)
    assert {s.layer for s in doc.metrics["assertion_strength"].surfaces} == {"api", "e2e"}
    assert doc.metrics["baseline_drift"].status == "evaluated"
    assert doc.metrics["baseline_drift"].value == pytest.approx(0.1)


def test_adversarial_yield_stays_pending_without_invented_gap() -> None:
    doc = _aggregate(adversarial_yield=None)
    assert doc.metrics["adversarial_yield"].status == "not_evaluated"
    assert any(
        board.code == "pending_nightly" and board.metric == "adversarial_yield" for board in doc.shortboards
    )
    assert not any(
        gap.metric == "adversarial_yield" and gap.code in {"collection_failed", "artifact_corrupt"}
        for gap in doc.collection_gaps
    )


def test_adversarial_yield_folds_evaluated_when_evidence_present() -> None:
    doc = _aggregate(adversarial_yield=_yield())
    entry = doc.metrics["adversarial_yield"]
    assert entry.status == "evaluated"
    assert entry.value == pytest.approx(2.0)
    assert entry.evidence == "adversarial-yield.json"
    assert entry.layer == "api"
    assert not any(b.code == "pending_nightly" and b.metric == "adversarial_yield" for b in doc.shortboards)


def test_adversarial_clean_holds_when_unclosed_count_is_zero() -> None:
    doc = _aggregate(adversarial_yield=_yield(unclosed_count=0, counterexample_ids=("CE-1",), value=1.0))
    clean = doc.metrics["adversarial_clean"]
    assert clean.status == "evaluated"
    assert clean.holds is True
    assert clean.layer == "cross"
    assert clean.evidence == "adversarial-yield.json"
    assert not any(b.code == "adversarial_open" and b.metric == "adversarial_clean" for b in doc.shortboards)


def test_adversarial_clean_false_when_unclosed_count_positive() -> None:
    doc = _aggregate(adversarial_yield=_yield(unclosed_count=1))
    clean = doc.metrics["adversarial_clean"]
    assert clean.status == "evaluated"
    assert clean.holds is False
    assert clean.layer == "cross"
    assert any(b.code == "adversarial_open" and b.metric == "adversarial_clean" for b in doc.shortboards)


def test_pending_yield_leaves_adversarial_clean_not_evaluated() -> None:
    """Absent yield must not invent adversarial_clean=true (boolean floor would not fire)."""
    doc = _aggregate(adversarial_yield=None)
    clean = doc.metrics["adversarial_clean"]
    assert clean.status == "not_evaluated"
    assert clean.holds is None
    assert not any(b.code == "adversarial_open" for b in doc.shortboards)


def test_collection_failed_yield_leaves_adversarial_clean_not_evaluated() -> None:
    gap = MetricCollectionGap(code="collection_failed", metric="adversarial_yield", detail="start_failed")
    doc = _aggregate(
        adversarial_yield=_yield(
            status="collection_failed",
            value=None,
            counterexample_ids=(),
            unclosed_count=0,
            collection_gaps=(gap,),
        ),
        floors={},
    )
    assert doc.metrics["adversarial_clean"].status == "not_evaluated"
    assert doc.metrics["adversarial_clean"].holds is None


def test_missing_evidence_is_not_evaluated_with_pending_nightly() -> None:
    doc = _aggregate(
        mutation=None,
        assertion_strength=None,
        baseline_drift=None,
        adversarial_yield=None,
        floors={},
    )
    for key in DEFAULT_NIGHTLY_KEYS:
        assert doc.metrics[key].status == "not_evaluated"
    pending = {board.metric for board in doc.shortboards if board.code == "pending_nightly"}
    assert pending == set(DEFAULT_NIGHTLY_KEYS)
    assert doc.floor_ratio is None
    assert doc.collection_gaps == ()


def test_not_evaluated_evidence_shortboards_are_preserved() -> None:
    doc = _aggregate(
        mutation=_mutation(
            status="not_evaluated",
            value=None,
            killed=0,
            survived=0,
            equivalent=0,
            tested=0,
            selected=0,
        ),
        assertion_strength=AssertionStrengthEvidence(
            schema_version="1",
            change_id=CHANGE_ID,
            batch_id=BATCH_ID,
            status="not_evaluated",
            shortboards=(MetricShortboard(code="pending_nightly", metric="assertion_strength"),),
        ),
        baseline_drift=BaselineDriftEvidence(
            schema_version="1",
            change_id=CHANGE_ID,
            batch_id=BATCH_ID,
            status="not_evaluated",
            drift_band=0.2,
            window=5,
            shortboards=(MetricShortboard(code="sample_insufficient", metric="baseline_drift"),),
        ),
        floors={},
    )
    assert doc.metrics["mutation_score"].status == "not_evaluated"
    assert any(b.code == "pending_nightly" and b.metric == "mutation_score" for b in doc.shortboards)
    assert any(b.code == "pending_nightly" and b.metric == "assertion_strength" for b in doc.shortboards)
    assert any(b.code == "sample_insufficient" and b.metric == "baseline_drift" for b in doc.shortboards)
    assert any(b.code == "pending_nightly" and b.metric == "baseline_drift" for b in doc.shortboards)


def test_collection_failed_evidence_propagates_gap() -> None:
    gap = MetricCollectionGap(code="collection_failed", metric="mutation_score", detail="start_failed")
    doc = _aggregate(
        mutation=_mutation(
            status="collection_failed",
            value=None,
            killed=0,
            survived=0,
            equivalent=0,
            tested=0,
            selected=0,
            collection_gaps=(gap,),
        ),
        floors={},
    )
    assert doc.metrics["mutation_score"].status == "collection_failed"
    assert any(g.code == "collection_failed" and g.metric == "mutation_score" for g in doc.collection_gaps)


def test_floor_ratio_uses_enabled_evaluated_numerics_only() -> None:
    doc = _aggregate()
    # assertion 0.875/1.0 → 0.875; baseline 0.1/0.2 → 0.5; mutation has no floor
    assert doc.floor_ratio == pytest.approx(0.5)


def test_below_floor_shortboards_are_attached_for_numeric_misses() -> None:
    doc = _aggregate()
    below = {b.metric for b in doc.shortboards if b.code == "below_floor"}
    assert "assertion_strength" in below
    assert "baseline_drift" in below


def test_boolean_floor_miss_is_checked_independently_without_entering_ratio() -> None:
    floors = {
        "assertion_strength": MetricFloor(target="value", min=0.5),
        "adversarial_clean": MetricFloor(target="holds", must_hold=True),
    }
    # Pending yield → clean stays not_evaluated; boolean floors never enter floor_ratio.
    doc = _aggregate(floors=floors)
    assert doc.metrics["adversarial_clean"].status == "not_evaluated"
    assert doc.floor_ratio == pytest.approx(1.0)
    assert not any(b.code == "below_floor" and b.metric == "adversarial_clean" for b in doc.shortboards)


def test_same_inputs_replay_to_identical_bytes_excluding_computed_at() -> None:
    a = _aggregate(computed_at=datetime(2026, 8, 5, 1, 0, tzinfo=UTC))
    b = _aggregate(computed_at=datetime(2026, 8, 5, 23, 0, tzinfo=UTC))
    assert a.replay_subtree() == b.replay_subtree()
    assert canonical_json_bytes(a.replay_subtree()) == canonical_json_bytes(b.replay_subtree())


def test_document_never_publishes_confidence_alias_for_floor_ratio() -> None:
    doc = _aggregate()
    dumped = doc.model_dump(mode="json")
    assert "floor_ratio" in dumped
    assert "confidence" not in dumped
    assert "confidence" not in dumped.get("metrics", {})
