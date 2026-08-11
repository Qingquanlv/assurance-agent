"""Nightly assertion-strength evidence (M2 Task 4) for aggregate-nightly."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from assurance_agent.artifacts.models.metrics import MetricScope, MetricShortboard
from assurance_agent.artifacts.models.pr_metric_evidence import (
    AssertionStrengthEvidence,
    AssertionStrengthSurfaceSlice,
    WeakAssertionLocator,
)


def _slice(
    layer: str,
    *,
    total: int,
    strong: int,
    weak_locators: tuple[str, ...] = (),
) -> AssertionStrengthSurfaceSlice:
    return AssertionStrengthSurfaceSlice(
        layer=layer,  # type: ignore[arg-type]
        declared=MetricScope.of(total=total, covered=strong, uncovered=weak_locators),
        value=None if total == 0 else strong / total,
        strong=strong,
        weak=total - strong,
    )


def test_evaluated_requires_both_api_and_e2e_slices() -> None:
    evidence = AssertionStrengthEvidence(
        schema_version="1",
        change_id="CH-1",
        batch_id="nightly",
        status="evaluated",
        value=0.5,
        declared=MetricScope.of(total=4, covered=2),
        surfaces=(
            _slice("api", total=2, strong=1, weak_locators=("tests/api/a.py::test_weak",)),
            _slice("e2e", total=2, strong=1, weak_locators=("tests/e2e/b.py::test_vis",)),
        ),
        weak_assertions=(
            WeakAssertionLocator(
                locator="tests/api/a.py::test_weak",
                surface="api",
                file="tests/api/a.py",
                function_name="test_weak",
                lineno=1,
                reasons=("status_200_only",),
            ),
            WeakAssertionLocator(
                locator="tests/e2e/b.py::test_vis",
                surface="e2e",
                file="tests/e2e/b.py",
                function_name="test_vis",
                lineno=1,
                reasons=("visibility_only",),
            ),
        ),
    )
    assert evidence.value == 0.5
    assert {s.layer for s in evidence.surfaces} == {"api", "e2e"}
    assert len(evidence.weak_assertions) == 2


def test_evaluated_without_both_surfaces_is_refused() -> None:
    with pytest.raises(ValidationError, match="api|e2e|surface"):
        AssertionStrengthEvidence(
            schema_version="1",
            change_id="CH-1",
            batch_id="nightly",
            status="evaluated",
            value=1.0,
            declared=MetricScope.of(total=1, covered=1),
            surfaces=(_slice("api", total=1, strong=1),),
        )


def test_pooled_value_must_match_surface_sum() -> None:
    with pytest.raises(ValidationError, match="value|declared|pool"):
        AssertionStrengthEvidence(
            schema_version="1",
            change_id="CH-1",
            batch_id="nightly",
            status="evaluated",
            value=0.9,
            declared=MetricScope.of(total=4, covered=2),
            surfaces=(
                _slice("api", total=2, strong=1),
                _slice("e2e", total=2, strong=1),
            ),
        )


def test_not_evaluated_carries_pending_nightly_and_no_surfaces() -> None:
    evidence = AssertionStrengthEvidence(
        schema_version="1",
        change_id="CH-1",
        batch_id="nightly",
        status="not_evaluated",
        value=None,
        declared=None,
        surfaces=(),
        shortboards=(MetricShortboard(code="pending_nightly", metric="assertion_strength"),),
    )
    assert evidence.surfaces == ()
    assert evidence.shortboards[0].code == "pending_nightly"


def test_not_evaluated_cannot_publish_value_or_surfaces() -> None:
    with pytest.raises(ValidationError):
        AssertionStrengthEvidence(
            schema_version="1",
            change_id="CH-1",
            batch_id="nightly",
            status="not_evaluated",
            value=0.5,
            declared=MetricScope.of(total=2, covered=1),
            surfaces=(_slice("api", total=1, strong=1), _slice("e2e", total=1, strong=0)),
        )
