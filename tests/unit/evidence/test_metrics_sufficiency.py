"""``evaluate_metrics_sufficiency`` — the auditable floors/cadence consumer."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from assurance_agent.artifacts.models.metrics import (
    MetricCollectionGap,
    MetricEntry,
    MetricScope,
    MetricsDocument,
)
from assurance_agent.artifacts.models.policy import EvidenceSufficiency, MetricCadenceSchedule, MetricFloor
from assurance_agent.artifacts.policy import load_policy_bytes
from assurance_agent.evidence.metrics_sufficiency import evaluate_metrics_sufficiency

DIGEST = "0" * 64
COMPUTED_AT = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)


def _entry(**overrides: Any) -> MetricEntry:
    payload: dict[str, Any] = {
        "layer": "api",
        "status": "evaluated",
        "value": 1.0,
        "declared": MetricScope.of(total=4, covered=4),
        "evidence": "constraint-coverage.json",
    }
    payload.update(overrides)
    return MetricEntry(**payload)


def _document(**overrides: Any) -> MetricsDocument:
    payload: dict[str, Any] = {
        "schema_version": "2",
        "change_id": "CH-1",
        "cadence": "pr",
        "computed_at": COMPUTED_AT,
        "risk_tier": "low",
        "risk_tier_lower_bound": "low",
        "risk_tier_declared": None,
        "risk_declaration_lowered": False,
        "risk_lowered_declarations": (),
        "metrics": {
            "constraint_coverage": _entry(
                value=0.6,
                declared=MetricScope.of(total=5, covered=3),
            )
        },
        "collection_gaps": (),
        "shortboards": (),
        "floor_ratio": None,
        "policy_digest": DIGEST,
    }
    payload.update(overrides)
    return MetricsDocument(**payload)


def _sufficiency(**overrides: Any) -> EvidenceSufficiency:
    base = load_policy_bytes(None, origin="packaged").evidence_sufficiency
    if not overrides:
        return base
    return base.model_copy(update=overrides)


def _raised_low_floor() -> dict[str, Any]:
    floors = {tier: dict(band) for tier, band in _sufficiency().floors.items()}
    floors["low"] = {
        **floors["low"],
        "constraint_coverage": MetricFloor(target="value", min=1.0),
    }
    return floors


def test_default_low_floor_passes_when_constraint_coverage_clears_min() -> None:
    decision = evaluate_metrics_sufficiency(_document(), _sufficiency())
    assert decision.verdict == "pass"
    assert decision.mutation_budget_seconds == 300
    assert decision.shortboards == ()


def test_raising_the_floor_turns_pass_into_needs_human() -> None:
    decision = evaluate_metrics_sufficiency(_document(), _sufficiency(floors=_raised_low_floor()))
    assert decision.verdict == "needs_human"
    assert [board.metric for board in decision.shortboards] == ["constraint_coverage"]
    assert decision.shortboards[0].code == "below_floor"


def test_warn_on_insufficient_passes_numeric_floor_miss_with_shortboards() -> None:
    decision = evaluate_metrics_sufficiency(
        _document(),
        _sufficiency(floors=_raised_low_floor(), on_insufficient="warn"),
    )
    assert decision.verdict == "pass"
    assert decision.shortboards[0].code == "below_floor"
    assert decision.shortboards[0].metric == "constraint_coverage"


def test_block_on_insufficient_stops_on_numeric_floor_miss() -> None:
    decision = evaluate_metrics_sufficiency(
        _document(),
        _sufficiency(floors=_raised_low_floor(), on_insufficient="block"),
    )
    assert decision.verdict == "stop"
    assert decision.shortboards[0].metric == "constraint_coverage"


def test_removing_the_metric_from_cadence_skips_the_numeric_floor() -> None:
    cadence = MetricCadenceSchedule(
        pr=["diff_coverage", "auth_matrix_coverage", "journey_coverage", "threshold_slack"],
        nightly=list(_sufficiency().cadence.nightly),
    )
    decision = evaluate_metrics_sufficiency(
        _document(), _sufficiency(floors=_raised_low_floor(), cadence=cadence)
    )
    assert decision.verdict == "pass"


def test_empty_cadence_schedule_is_skipped() -> None:
    cadence = MetricCadenceSchedule(pr=[], nightly=list(_sufficiency().cadence.nightly))
    decision = evaluate_metrics_sufficiency(_document(), _sufficiency(cadence=cadence))
    assert decision.verdict == "skipped"


def test_boolean_floor_miss_stops_even_when_not_in_cadence() -> None:
    document = _document(
        risk_tier="critical",
        risk_tier_lower_bound="critical",
        metrics={
            "adversarial_clean": _entry(
                layer="cross",
                value=None,
                declared=None,
                holds=False,
                evidence="adversarial.json",
            )
        },
    )
    decision = evaluate_metrics_sufficiency(document, _sufficiency())
    assert decision.verdict == "stop"


def test_critical_adversarial_clean_false_stops() -> None:
    """M3 Task 2: critical tier + open counterexamples → stop."""
    document = _document(
        risk_tier="critical",
        risk_tier_lower_bound="critical",
        cadence="nightly",
        metrics={
            "adversarial_clean": _entry(
                layer="cross",
                value=None,
                declared=None,
                holds=False,
                evidence="adversarial-yield.json",
            )
        },
    )
    assert evaluate_metrics_sufficiency(document, _sufficiency()).verdict == "stop"


def test_non_critical_adversarial_clean_false_needs_human() -> None:
    """M3 Task 2: lower tiers keep the hard rule visible as needs_human, not stop."""
    document = _document(
        risk_tier="low",
        risk_tier_lower_bound="low",
        cadence="nightly",
        metrics={
            "adversarial_clean": _entry(
                layer="cross",
                value=None,
                declared=None,
                holds=False,
                evidence="adversarial-yield.json",
            )
        },
    )
    assert evaluate_metrics_sufficiency(document, _sufficiency()).verdict == "needs_human"


def test_not_evaluated_adversarial_clean_is_not_a_boolean_floor_fail() -> None:
    """Pending clean must not fire the boolean hard rule (no invented holds=True either)."""
    document = _document(
        risk_tier="critical",
        risk_tier_lower_bound="critical",
        cadence="nightly",
        metrics={
            "adversarial_clean": _entry(
                layer="cross",
                status="not_evaluated",
                value=None,
                declared=None,
                evidence="",
            )
        },
    )
    decision = evaluate_metrics_sufficiency(document, _sufficiency())
    assert decision.verdict == "pass"
    assert decision.shortboards == ()


def test_empty_cadence_does_not_skip_evaluated_boolean_stop() -> None:
    """Boolean floors are deterministic checks; empty PR schedule must not swallow stop."""
    cadence = MetricCadenceSchedule(pr=[], nightly=list(_sufficiency().cadence.nightly))
    document = _document(
        risk_tier="critical",
        risk_tier_lower_bound="critical",
        metrics={
            "adversarial_clean": _entry(
                layer="cross",
                value=None,
                declared=None,
                holds=False,
                evidence="adversarial.json",
            )
        },
    )
    decision = evaluate_metrics_sufficiency(document, _sufficiency(cadence=cadence))
    assert decision.verdict == "stop"


def test_collection_gaps_route_to_reject() -> None:
    """§12.5: typed collection gaps are fail-closed, never SKIPPED.

    ``reject`` (not ``needs_human``): "no number to judge" blocks release but
    keeps flowing to report/archive/retro (§ fast-loop routing), rather than
    being eligible for a blanket ``accept_risk`` rubber stamp.
    """
    document = _document(
        collection_gaps=(
            MetricCollectionGap(
                code="entity_without_constraints",
                metric="constraint_coverage",
                subject="entities.audit_log",
            ),
        )
    )
    decision = evaluate_metrics_sufficiency(document, _sufficiency())
    assert decision.verdict == "reject"


def test_not_evaluated_is_not_a_floor_fail() -> None:
    """§12.5 / §7: pending measurement is not a gap and does not fail the floor."""
    document = _document(
        metrics={
            "constraint_coverage": _entry(
                status="not_evaluated",
                value=None,
                declared=None,
                evidence="",
            )
        }
    )
    decision = evaluate_metrics_sufficiency(
        document, _sufficiency(floors=_raised_low_floor(), on_insufficient="block")
    )
    assert decision.verdict == "pass"
    assert decision.shortboards == ()


def test_collection_failed_is_reject_not_floor_miss() -> None:
    """collection_failed is fail-closed reject; distinct from not_evaluated."""
    document = _document(
        metrics={
            "constraint_coverage": _entry(
                status="collection_failed",
                value=None,
                declared=None,
                evidence="",
            )
        },
        collection_gaps=(MetricCollectionGap(code="collection_failed", metric="constraint_coverage"),),
    )
    # collection_gaps already forces reject; pin that collection_failed is not
    # reinterpreted as a warn-band floor miss.
    decision = evaluate_metrics_sufficiency(
        document, _sufficiency(floors=_raised_low_floor(), on_insufficient="warn")
    )
    assert decision.verdict == "reject"
    assert decision.shortboards == ()


def test_mutation_budget_is_echoed_from_policy() -> None:
    """Budget is copied onto the decision; exceeding it never flips the verdict (§5-B1)."""
    decision = evaluate_metrics_sufficiency(_document(), _sufficiency(mutation_budget_seconds=120))
    assert decision.mutation_budget_seconds == 120
    assert decision.verdict == "pass"


def test_boolean_floor_without_holds_is_reject_not_needs_human() -> None:
    """A boolean floor over an entry that never published ``holds`` has no verdict basis.

    ``MetricsDocument`` pins each packaged key to its kind, so this only arises
    when a policy floor declares ``target: holds`` against a numeric metric.
    Same family as a numeric collection gap: reject, not a human-stampable
    needs_human.
    """
    floors = {tier: dict(band) for tier, band in _sufficiency().floors.items()}
    floors["low"] = {
        **floors["low"],
        "constraint_coverage": MetricFloor(target="holds", must_hold=True),
    }
    decision = evaluate_metrics_sufficiency(_document(), _sufficiency(floors=floors))
    assert decision.verdict == "reject"
    assert decision.shortboards == ()


def test_empty_touched_scope_fails_closed_not_vacuous_pass() -> None:
    """touched total=0 (value None) is evaluated-but-unmeasurable → reject.

    The packaged floors target ``touched`` for auth_matrix_coverage on every tier;
    an explicitly empty touched scope (change touched no declared cell) must never
    read as a cleared 1.0 floor.
    """
    document = _document(
        metrics={
            "auth_matrix_coverage": _entry(
                value=0.0,
                declared=MetricScope.of(total=2, covered=0, uncovered=("c1", "c2")),
                touched=MetricScope.of(total=0, covered=0),
                evidence="auth-matrix.json",
            )
        }
    )
    decision = evaluate_metrics_sufficiency(document, _sufficiency())
    assert decision.verdict == "reject"
    assert decision.shortboards == ()
