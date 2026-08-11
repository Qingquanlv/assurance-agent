"""M2 Task 6: ``compute_floor_ratio`` over enabled + evaluated numeric metrics."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from assurance_agent.artifacts.models.metrics import (
    MetricEntry,
    MetricKey,
    MetricScope,
    MetricsDocument,
    MetricShortboard,
    MetricSurface,
)
from assurance_agent.artifacts.models.policy import MetricFloor
from assurance_agent.evidence.metrics_sufficiency import compute_floor_ratio

DIGEST = "0" * 64
COMPUTED_AT = datetime(2026, 8, 5, 11, 0, tzinfo=UTC)


def _surface(layer: str, total: int, covered: int) -> MetricSurface:
    return MetricSurface(
        layer=layer,  # type: ignore[arg-type]
        value=covered / total,
        declared=MetricScope.of(total=total, covered=covered),
        evidence=f"assertion-strength-{layer}.json",
    )


def _assertion_strength(api: tuple[int, int], e2e: tuple[int, int]) -> MetricEntry:
    total, covered = api[0] + e2e[0], api[1] + e2e[1]
    return MetricEntry(
        layer="cross",
        status="evaluated",
        value=covered / total,
        declared=MetricScope.of(total=total, covered=covered),
        surfaces=(_surface("api", *api), _surface("e2e", *e2e)),
        evidence="assertion-strength.json",
    )


def _scalar(layer: str, value: float, *, evidence: str) -> MetricEntry:
    return MetricEntry(layer=layer, status="evaluated", value=value, evidence=evidence)  # type: ignore[arg-type]


def _document(metrics: dict[str, MetricEntry], **overrides: Any) -> MetricsDocument:
    payload: dict[str, Any] = {
        "schema_version": "2",
        "change_id": "CH-FLOOR-1",
        "cadence": "nightly",
        "computed_at": COMPUTED_AT,
        "risk_tier": "medium",
        "risk_tier_lower_bound": "medium",
        "risk_tier_declared": None,
        "risk_declaration_lowered": False,
        "risk_lowered_declarations": (),
        "metrics": metrics,
        "collection_gaps": (),
        "shortboards": (),
        "floor_ratio": None,
        "policy_digest": DIGEST,
    }
    payload.update(overrides)
    return MetricsDocument(**payload)


def test_floor_ratio_is_min_of_capped_actual_over_floor() -> None:
    floors: dict[MetricKey, MetricFloor] = {
        "assertion_strength": MetricFloor(target="value", min=1.0),
        "baseline_drift": MetricFloor(target="value", min=0.2),
    }
    # assertion 0.5/1.0 → 0.5; baseline 0.1/0.2 → 0.5; min → 0.5
    document = _document(
        {
            "assertion_strength": _assertion_strength((2, 1), (2, 1)),
            "baseline_drift": _scalar("performance", 0.1, evidence="baseline-drift.json"),
        }
    )
    assert compute_floor_ratio(document, floors) == pytest.approx(0.5)


def test_floor_ratio_caps_each_metric_at_one() -> None:
    floors: dict[MetricKey, MetricFloor] = {"assertion_strength": MetricFloor(target="value", min=0.5)}
    document = _document({"assertion_strength": _assertion_strength((2, 2), (2, 2))})
    assert compute_floor_ratio(document, floors) == pytest.approx(1.0)


def test_not_evaluated_and_null_are_excluded_from_floor_ratio() -> None:
    floors: dict[MetricKey, MetricFloor] = {
        "assertion_strength": MetricFloor(target="value", min=1.0),
        "mutation_score": MetricFloor(target="value", min=0.8),
    }
    document = _document(
        {
            "assertion_strength": _assertion_strength((5, 4), (5, 5)),  # 9/10
            "mutation_score": MetricEntry(layer="backend", status="not_evaluated"),
        },
        shortboards=(MetricShortboard(code="pending_nightly", metric="mutation_score"),),
    )
    assert compute_floor_ratio(document, floors) == pytest.approx(0.9)


def test_metric_without_floor_is_not_enabled_and_is_excluded() -> None:
    """mutation_score defaults to no floor (§5-B1); evaluated ≠ enabled."""
    floors: dict[MetricKey, MetricFloor] = {"assertion_strength": MetricFloor(target="value", min=1.0)}
    document = _document(
        {
            "assertion_strength": _assertion_strength((1, 1), (1, 1)),
            "mutation_score": _scalar("backend", 0.1, evidence="mutation.json"),
        }
    )
    assert compute_floor_ratio(document, floors) == pytest.approx(1.0)


def test_boolean_floors_are_ignored_by_floor_ratio() -> None:
    floors: dict[MetricKey, MetricFloor] = {
        "adversarial_clean": MetricFloor(target="holds", must_hold=True),
        "assertion_strength": MetricFloor(target="value", min=1.0),
    }
    document = _document(
        {
            "assertion_strength": _assertion_strength((2, 1), (2, 2)),  # 3/4
            "adversarial_clean": MetricEntry(
                layer="cross",
                status="evaluated",
                holds=False,
                evidence="adversarial-yield.json",
            ),
        },
        risk_tier="critical",
        risk_tier_lower_bound="critical",
    )
    assert compute_floor_ratio(document, floors) == pytest.approx(0.75)


def test_no_enabled_evaluated_numeric_yields_none() -> None:
    floors: dict[MetricKey, MetricFloor] = {"assertion_strength": MetricFloor(target="value", min=1.0)}
    document = _document(
        {
            "assertion_strength": MetricEntry(layer="cross", status="not_evaluated"),
            "adversarial_yield": MetricEntry(layer="api", status="not_evaluated"),
        },
        shortboards=(
            MetricShortboard(code="pending_nightly", metric="assertion_strength"),
            MetricShortboard(code="pending_nightly", metric="adversarial_yield"),
        ),
    )
    assert compute_floor_ratio(document, floors) is None


def test_compute_floor_ratio_never_named_confidence() -> None:
    """§3.10 / §12.14 naming guard: floor_ratio must not be published as confidence."""
    assert compute_floor_ratio.__name__ != "compute_confidence"
    assert "confidence" not in compute_floor_ratio.__name__
