"""Risk tier resolution (spec §7): a mechanical floor an LLM can only raise.

`risk.level` is authored by the case-design model and, until now, validated by
nothing — the observed run RET-api-management-20260804-115008 labelled a P0 case
`medium` and the review model waved the conflict through as informational. So the
tier that selects a floor band is not the declaration: it is the maximum of the
declaration and a mechanical bound derived from `priority`/`severity`, which the
authoring model does not control.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, get_args

import pytest

from assurance_agent.artifacts.models.cases import CaseEntry, CasePriority, CaseSeverity
from assurance_agent.artifacts.models.common import RiskTier
from assurance_agent.artifacts.models.metrics import (
    MetricEntry,
    MetricKey,
    MetricScope,
    MetricsDocument,
    RiskTierFacts,
)
from assurance_agent.evidence.risk_tier import mechanical_lower_bound, resolve_risk_tier

# What the factory must carry over from a resolution, and the field each fact is
# published under. The document's whole `risk_*` surface, so a fact added to one
# side and not wired through the factory fails
# `test_the_factory_hands_over_every_risk_fact_the_resolution_carries`.
RISK_FACTS = {
    "tier": "risk_tier",
    "lower_bound": "risk_tier_lower_bound",
    "declared": "risk_tier_declared",
    "declaration_lowered": "risk_declaration_lowered",
    "lowered_declarations": "risk_lowered_declarations",
}


def _metrics() -> dict[MetricKey, MetricEntry]:
    return {
        "constraint_coverage": MetricEntry(
            layer="api",
            status="evaluated",
            value=1.0,
            declared=MetricScope.of(total=4, covered=4),
            evidence="constraint-coverage.json",
        )
    }


def _case(**overrides: Any) -> CaseEntry:
    payload: dict[str, Any] = {
        "case_id": "TC_API_001",
        "title": "create api metadata",
        "status": "active",
        "priority": "P1",
        "severity": "major",
        "type": "API",
        "module": "system.api",
    }
    payload.update(overrides)
    return CaseEntry.model_validate(payload)


# --------------------------------------------------------------------------- #
# the mechanical bound
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("priority", "expected"),
    [("P0", "high"), ("P1", "medium"), ("P2", "low"), ("P3", "low")],
)
def test_priority_sets_a_lower_bound(priority: str, expected: str) -> None:
    assert mechanical_lower_bound([_case(priority=priority, severity="minor")]) == expected


@pytest.mark.parametrize(
    ("severity", "expected"),
    [("blocker", "high"), ("critical", "high"), ("major", "medium"), ("minor", "low")],
)
def test_severity_sets_a_lower_bound(severity: str, expected: str) -> None:
    assert mechanical_lower_bound([_case(priority="P3", severity=severity)]) == expected


def test_the_bound_is_the_maximum_over_both_axes_and_over_every_case() -> None:
    cases = [
        _case(priority="P3", severity="minor"),
        _case(case_id="TC_API_002", priority="P2", severity="blocker"),
    ]
    assert mechanical_lower_bound(cases) == "high"


def test_every_priority_and_severity_value_has_a_bound() -> None:
    """A new enum value must be given a bound here rather than defaulting to `low`."""
    for priority in get_args(CasePriority):
        assert mechanical_lower_bound([_case(priority=priority)]) in get_args(RiskTier)
    for severity in get_args(CaseSeverity):
        assert mechanical_lower_bound([_case(severity=severity)]) in get_args(RiskTier)


def test_no_cases_bounds_at_low() -> None:
    assert mechanical_lower_bound([]) == "low"
    assert resolve_risk_tier([]).tier == "low"


# --------------------------------------------------------------------------- #
# the declaration may only raise
# --------------------------------------------------------------------------- #


def test_a_p0_case_declared_medium_still_resolves_to_high() -> None:
    """The pinned regression: the exact downgrade an LLM was observed to author."""
    resolution = resolve_risk_tier([_case(priority="P0", severity="major", risk={"level": "medium"})])
    assert resolution.tier == "high"
    assert resolution.lower_bound == "high"
    assert resolution.declared == "medium"
    assert resolution.declaration_lowered is True
    assert resolution.declaration_raised is False


def test_a_declaration_above_the_bound_is_adopted() -> None:
    resolution = resolve_risk_tier([_case(priority="P2", severity="minor", risk={"level": "critical"})])
    assert resolution.tier == "critical"
    assert resolution.lower_bound == "low"
    assert resolution.declaration_raised is True
    assert resolution.declaration_lowered is False


def test_the_highest_declaration_wins_across_cases() -> None:
    cases = [
        _case(priority="P3", severity="minor", risk={"level": "low"}),
        _case(case_id="TC_API_002", priority="P3", severity="minor", risk={"level": "high"}),
    ]
    assert resolve_risk_tier(cases).tier == "high"


# --------------------------------------------------------------------------- #
# lowering is audited per case, because the aggregate hides it
# --------------------------------------------------------------------------- #


def test_one_case_declaring_critical_cannot_hide_another_declaring_low() -> None:
    """The aggregate signal is unusable on its own.

    `max(declared)` is `critical`, above the bound, so a flag derived from the
    aggregate would say nothing was lowered — while the P0 case that asked for
    `low` is exactly the authoring behaviour §2.3 wants surfaced.
    """
    cases = [
        _case(case_id="TC_API_001", priority="P0", severity="major", risk={"level": "low"}),
        _case(case_id="TC_API_002", priority="P2", severity="minor", risk={"level": "critical"}),
    ]
    resolution = resolve_risk_tier(cases)
    assert resolution.declared == "critical"
    assert resolution.tier == "critical"
    assert resolution.declaration_lowered is True
    assert [lowered.case_id for lowered in resolution.lowered_declarations] == ["TC_API_001"]


def test_the_audit_names_the_case_and_both_levels_it_disagreed_about() -> None:
    resolution = resolve_risk_tier([_case(priority="P0", severity="major", risk={"level": "medium"})])
    (lowered,) = resolution.lowered_declarations
    assert (lowered.case_id, lowered.declared, lowered.lower_bound) == ("TC_API_001", "medium", "high")


def test_each_case_is_audited_against_its_own_bound_not_the_aggregate() -> None:
    """A P3 case declaring `low` beside a P0 case has lowered nothing.

    Auditing against the change-wide bound (`high` here) would report every
    modest case as underselling risk, and a signal that fires on correct
    authoring is a signal a retro learns to ignore.
    """
    cases = [
        _case(case_id="TC_API_001", priority="P0", severity="major"),
        _case(case_id="TC_API_002", priority="P3", severity="minor", risk={"level": "low"}),
    ]
    resolution = resolve_risk_tier(cases)
    assert resolution.lower_bound == "high"
    assert resolution.lowered_declarations == ()
    assert resolution.declaration_lowered is False


def test_the_audit_is_ordered_by_case_id() -> None:
    cases = [
        _case(case_id="TC_API_009", priority="P0", severity="major", risk={"level": "low"}),
        _case(case_id="TC_API_002", priority="P0", severity="major", risk={"level": "medium"}),
    ]
    resolution = resolve_risk_tier(cases)
    assert [lowered.case_id for lowered in resolution.lowered_declarations] == [
        "TC_API_002",
        "TC_API_009",
    ]


def test_a_case_without_a_risk_block_resolves_to_its_mechanical_bound() -> None:
    resolution = resolve_risk_tier([_case(priority="P0")])
    assert resolution.declared is None
    assert resolution.tier == "high"
    assert resolution.declaration_lowered is False
    assert resolution.declaration_raised is False


def test_the_resolution_is_immutable() -> None:
    resolution = resolve_risk_tier([_case()])
    with pytest.raises(AttributeError):
        resolution.tier = "critical"  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# the seam to the published document
# --------------------------------------------------------------------------- #


def test_a_resolution_is_what_the_metrics_document_factory_asks_for() -> None:
    """The artifact layer takes these facts structurally rather than by import."""
    assert isinstance(resolve_risk_tier([_case()]), RiskTierFacts)


def test_publishing_a_resolution_preserves_the_p0_medium_downgrade_as_high() -> None:
    """End to end: an authored `medium` on a P0 case cannot select medium floors.

    The two halves are pinned separately above and in the artifact tests; this is
    the one test that walks the whole path a producer takes, because the factory is
    the only place the risk fields are derived rather than retyped.
    """
    resolution = resolve_risk_tier([_case(priority="P0", severity="major", risk={"level": "medium"})])
    document = MetricsDocument.of(
        risk=resolution,
        change_id="CH-API-001",
        cadence="pr",
        computed_at=datetime(2026, 8, 4, 9, 30, tzinfo=UTC),
        metrics=_metrics(),
        policy_digest="d" * 64,
    )
    assert document.risk_tier == "high"
    assert document.risk_tier_lower_bound == "high"
    assert document.risk_tier_declared == "medium"
    assert document.risk_declaration_lowered is True
    assert [lowered.case_id for lowered in document.risk_lowered_declarations] == ["TC_API_001"]


def test_the_factory_hands_over_every_risk_fact_the_resolution_carries() -> None:
    """The handoff is the whole contract, so it is checked field by field.

    The document cannot recompute a mechanical bound — it has no cases — so a fact
    the resolution knows and the factory drops would be silently unpublishable. The
    second assertion is what makes that failure loud: a new `risk_*` field on the
    document (or a renamed one) is red here until it is wired through.
    """
    resolution = resolve_risk_tier(
        [
            _case(case_id="TC_API_001", priority="P0", severity="major", risk={"level": "low"}),
            _case(case_id="TC_API_002", priority="P2", severity="minor", risk={"level": "critical"}),
        ]
    )
    document = MetricsDocument.of(
        risk=resolution,
        change_id="CH-API-001",
        cadence="pr",
        computed_at=datetime(2026, 8, 4, 9, 30, tzinfo=UTC),
        metrics=_metrics(),
        policy_digest="d" * 64,
    )
    for fact, field in RISK_FACTS.items():
        assert getattr(document, field) == getattr(resolution, fact), field
    published = {name for name in MetricsDocument.model_fields if name.startswith("risk_")}
    assert published == set(RISK_FACTS.values())
