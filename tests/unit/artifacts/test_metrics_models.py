"""`inspect/metrics.json` — the verification metric vector the metrics gate reads.

The document is what `aggregate-metrics` publishes and what
`metrics-sufficiency-gate` routes on, so these tests are about what the
*document* can and cannot say: a metric key it cannot invent, a floor key variant
it must not grow (spec §8/§9's closed enum), an answer shaped unlike the one its
own metric card defines, a coverage ratio it cannot publish without the tally it
came from, a two-classifier metric (§5-B2) it cannot report as one number, a
measurement it cannot claim without a denominator, a whole-metric collection
failure it cannot fold into silence (spec §6's fail-open), a partial failure it
must *not* be forced to discard, and a risk tier no producer can restate wrongly
or quietly undersell (spec §7).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, get_args

import pytest
from pydantic import ValidationError

from assurance_agent.artifacts.models.common import RISK_TIER_ORDER, RiskTier
from assurance_agent.artifacts.models.metrics import (
    ARTIFACT_REL_BY_CADENCE,
    METRIC_KEYS,
    METRIC_KINDS,
    METRIC_LAYERS,
    METRIC_SHAPES,
    METRIC_SURFACES,
    NIGHTLY_METRICS_REL,
    PR_METRICS_REL,
    SUBJECT_SCOPED_GAP_CODES,
    WHOLE_METRIC_GAP_CODES,
    MetricCadence,
    MetricCollectionGap,
    MetricCollectionGapCode,
    MetricEntry,
    MetricKey,
    MetricLayer,
    MetricScope,
    MetricShape,
    MetricShortboard,
    MetricShortboardCode,
    MetricsDocument,
    MetricStatus,
    MetricSurface,
    RiskDeclarationLowered,
)

COMPUTED_AT = datetime(2026, 8, 4, 9, 30, tzinfo=UTC)
DIGEST = "d" * 64
SCOPED_RATIO_KEYS = tuple(key for key, shape in METRIC_SHAPES.items() if shape == "scoped_ratio")
SCALAR_KEYS = tuple(key for key, shape in METRIC_SHAPES.items() if shape == "scalar")


@dataclass(frozen=True)
class _Risk:
    """A `RiskTierFacts` stand-in, so the factory can be exercised without the
    evidence layer. The real seam is pinned in `tests/unit/evidence/test_risk_tier.py`."""

    tier: RiskTier
    lower_bound: RiskTier
    declared: RiskTier | None
    declaration_lowered: bool
    lowered_declarations: tuple[RiskDeclarationLowered, ...] = field(default=())


def _entry(**overrides: Any) -> MetricEntry:
    """An evaluated `constraint_coverage` entry: a ratio *and* the tally behind it."""
    payload: dict[str, Any] = {
        "layer": "api",
        "status": "evaluated",
        "value": 1.0,
        "declared": MetricScope.of(total=4, covered=4),
        "evidence": "constraint-coverage.json",
    }
    payload.update(overrides)
    return MetricEntry(**payload)


def _unmeasured(**overrides: Any) -> MetricEntry:
    """An entry that measured nothing — no value, no tally, nothing to point at."""
    payload: dict[str, Any] = {
        "layer": "api",
        "status": "not_evaluated",
        "value": None,
        "declared": None,
        "evidence": "",
    }
    payload.update(overrides)
    return MetricEntry(**payload)


def _surface(layer: str, total: int, covered: int, **overrides: Any) -> MetricSurface:
    payload: dict[str, Any] = {
        "layer": layer,
        "value": covered / total,
        "declared": MetricScope.of(total=total, covered=covered),
        "evidence": f"assertion-strength-{layer}.json",
    }
    payload.update(overrides)
    return MetricSurface(**payload)


def _assertion_strength(
    api: tuple[int, int] = (10, 6), e2e: tuple[int, int] = (5, 2), **overrides: Any
) -> MetricEntry:
    """The §5-B2 metric: one classifier per surface, pooled into the published value."""
    total, covered = api[0] + e2e[0], api[1] + e2e[1]
    payload: dict[str, Any] = {
        "layer": "cross",
        "status": "evaluated",
        "value": covered / total,
        "declared": MetricScope.of(total=total, covered=covered),
        "surfaces": (_surface("api", *api), _surface("e2e", *e2e)),
        "evidence": "assertion-strength.json",
    }
    payload.update(overrides)
    return MetricEntry(**payload)


def _document(**overrides: Any) -> MetricsDocument:
    payload: dict[str, Any] = {
        "schema_version": "2",
        "change_id": "CH-API-001",
        "cadence": "pr",
        "computed_at": COMPUTED_AT,
        "risk_tier": "medium",
        "risk_tier_lower_bound": "medium",
        "risk_tier_declared": None,
        "risk_declaration_lowered": False,
        "risk_lowered_declarations": (),
        "metrics": {"constraint_coverage": _entry()},
        "collection_gaps": (),
        "shortboards": (),
        "floor_ratio": None,
        "policy_digest": DIGEST,
    }
    payload.update(overrides)
    return MetricsDocument(**payload)


# --------------------------------------------------------------------------- #
# one closed key vocabulary, shared by floors, cadence and this artifact
# --------------------------------------------------------------------------- #


def test_the_metric_key_vocabulary_is_exactly_these_ten() -> None:
    """Pinned as an equality: `floors` keys and cadence lists must be a subset of
    this set (§12.7), so a key added without a floor decision has to come here."""
    assert set(get_args(MetricKey)) == {
        "diff_coverage",
        "constraint_coverage",
        "auth_matrix_coverage",
        "journey_coverage",
        "baseline_drift",
        "mutation_score",
        "assertion_strength",
        "threshold_slack",
        "adversarial_yield",
        "adversarial_clean",
    }


def test_the_key_vocabulary_has_no_duplicates_and_is_exported_for_guards() -> None:
    keys = get_args(MetricKey)
    assert len(keys) == len(set(keys))
    assert METRIC_KEYS == keys


def test_no_floor_key_variant_shadows_a_canonical_key() -> None:
    """Spec §8's `*_touched` floor keys are a second vocabulary; §9 asks for one.

    A `constraint_coverage_touched` key would make `floors`/`cadence`/artifact key
    sets non-closed (v1 problem #7) and would let a floor be declared against a
    metric the artifact never publishes. A floor names the canonical key and the
    scope it targets instead.
    """
    for key in get_args(MetricKey):
        assert not key.endswith("_touched"), key


def test_the_status_vocabulary_is_exactly_these_four() -> None:
    """Spec §6: folding collection failure into SKIPPED is a silent fail-open."""
    assert set(get_args(MetricStatus)) == {
        "evaluated",
        "not_evaluated",
        "skipped",
        "collection_failed",
    }


def test_the_gap_vocabulary_is_exactly_these_five() -> None:
    assert set(get_args(MetricCollectionGapCode)) == {
        "collection_failed",
        "artifact_corrupt",
        "identity_mismatch",
        "entity_without_constraints",
        "property_unknown_key",
    }


def test_the_shortboard_vocabulary_is_exactly_these_eight() -> None:
    assert set(get_args(MetricShortboardCode)) == {
        "below_floor",
        "pending_nightly",
        "threshold_slack_out_of_band",
        "baseline_drift_out_of_band",
        "sample_insufficient",
        "mutation_budget_exceeded",
        "adversarial_open",
        "quarantined_excluded",
    }


def test_the_layer_shape_and_cadence_vocabularies_are_exact() -> None:
    assert set(get_args(MetricLayer)) == {"api", "e2e", "fuzz", "performance", "backend", "cross"}
    assert set(get_args(MetricShape)) == {"scoped_ratio", "scalar", "boolean"}
    assert set(get_args(MetricCadence)) == {"pr", "nightly"}


def test_the_gap_codes_are_partitioned_by_scale() -> None:
    """A code that can only mean one scale is classified once, here.

    `identity_mismatch` is deliberately in neither set: a mismatched artifact
    sinks the metric, a mismatched row sinks one subject, and the producer says
    which by whether it can name a subject.
    """
    codes = set(get_args(MetricCollectionGapCode))
    assert WHOLE_METRIC_GAP_CODES < codes
    assert SUBJECT_SCOPED_GAP_CODES < codes
    assert not (WHOLE_METRIC_GAP_CODES & SUBJECT_SCOPED_GAP_CODES)
    assert codes - WHOLE_METRIC_GAP_CODES - SUBJECT_SCOPED_GAP_CODES == {"identity_mismatch"}


# --------------------------------------------------------------------------- #
# every key declares the shape of its answer, its layer and its surfaces
# --------------------------------------------------------------------------- #


def test_the_shape_of_every_key_is_pinned() -> None:
    """The whole mapping, as an equality, because each entry is a judgement.

    The three coverage ratios (§5-A2/A3/A4) are proportions of an enumerable
    closed key set, and §9 publishes their tallies. The scalars are the metrics
    whose cards define them as a magnitude with detail in their own artifact:
    diff coverage (A1, context only, never gates), baseline drift (A5, relative
    regression), mutation score (B1, budget-capped sampling whose survivors go to
    the report), threshold slack (B5, `threshold / measured`) and adversarial
    yield (B3, counterexamples per round). `assertion_strength` (B2) is a ratio
    over assertion populations, pooled from its per-surface classifiers.
    """
    assert dict(METRIC_SHAPES) == {
        "diff_coverage": "scalar",
        "constraint_coverage": "scoped_ratio",
        "auth_matrix_coverage": "scoped_ratio",
        "journey_coverage": "scoped_ratio",
        "baseline_drift": "scalar",
        "mutation_score": "scalar",
        "assertion_strength": "scoped_ratio",
        "threshold_slack": "scalar",
        "adversarial_yield": "scalar",
        "adversarial_clean": "boolean",
    }


def test_every_key_declares_a_shape_a_kind_a_layer_set_and_its_surfaces() -> None:
    """Totality, so a new key cannot default into "scalar, any layer"."""
    keys = set(get_args(MetricKey))
    assert set(METRIC_SHAPES) == keys
    assert set(METRIC_KINDS) == keys
    assert set(METRIC_LAYERS) == keys
    assert set(METRIC_SURFACES) == keys
    for key, layers in METRIC_LAYERS.items():
        assert layers, key
        assert layers <= set(get_args(MetricLayer)), key
    for key, surfaces in METRIC_SURFACES.items():
        assert surfaces <= set(get_args(MetricLayer)), key


def test_the_kind_of_a_key_follows_its_shape() -> None:
    """Kind is derived, not declared twice: only a boolean shape answers with `holds`."""
    for key, shape in METRIC_SHAPES.items():
        assert METRIC_KINDS[key] == ("boolean" if shape == "boolean" else "numeric"), key
    assert {key for key, kind in METRIC_KINDS.items() if kind == "boolean"} == {"adversarial_clean"}


def test_the_metric_cards_layer_assignments_are_declared() -> None:
    """§5: E2E and performance own their families; B2's aggregate spans surfaces;
    B3 follows whichever search layer produced the counterexamples."""
    assert METRIC_LAYERS["constraint_coverage"] == frozenset({"api"})
    assert METRIC_LAYERS["journey_coverage"] == frozenset({"e2e"})
    assert METRIC_LAYERS["threshold_slack"] == frozenset({"performance"})
    assert METRIC_LAYERS["diff_coverage"] == frozenset({"backend"})
    assert METRIC_LAYERS["assertion_strength"] == frozenset({"cross"})
    assert METRIC_LAYERS["adversarial_yield"] == frozenset({"api", "e2e", "fuzz"})


def test_only_the_two_classifier_metric_declares_surfaces() -> None:
    assert METRIC_SURFACES["assertion_strength"] == frozenset({"api", "e2e"})
    assert {key for key, surfaces in METRIC_SURFACES.items() if surfaces} == {"assertion_strength"}


def test_a_numeric_metric_may_not_answer_with_holds() -> None:
    """A `holds` on a numeric key is a metric that never enters `floor_ratio` and
    therefore never falls short of anything."""
    with pytest.raises(ValidationError):
        _document(metrics={"constraint_coverage": _entry(value=None, declared=None, holds=True)})


def test_a_boolean_metric_may_not_answer_with_value() -> None:
    with pytest.raises(ValidationError):
        _document(
            metrics={"adversarial_clean": _entry(layer="cross", value=1.0, declared=None, evidence="a.json")}
        )


def test_an_entry_may_not_claim_a_layer_its_metric_is_not_collected_on() -> None:
    with pytest.raises(ValidationError):
        _document(metrics={"journey_coverage": _entry(layer="api")})


def test_a_boolean_metric_records_a_check_that_did_not_hold() -> None:
    """`holds=False` is a measurement, not an absence: the check ran and failed."""
    document = _document(
        metrics={
            "adversarial_clean": _entry(
                layer="cross",
                value=None,
                declared=None,
                holds=False,
                evidence="adversarial-yield.json",
            )
        }
    )
    assert document.metrics["adversarial_clean"].holds is False


def test_holds_false_is_a_measurement_and_so_needs_an_evaluated_status() -> None:
    with pytest.raises(ValidationError):
        _entry(status="not_evaluated", value=None, declared=None, holds=False)


# --------------------------------------------------------------------------- #
# a scoped ratio publishes its denominator; a scalar has none
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("key", SCOPED_RATIO_KEYS)
def test_an_evaluated_coverage_ratio_may_not_publish_a_bare_value(key: MetricKey) -> None:
    """A bare `1.0` is a coverage claim with nothing to audit it against.

    It is also how "0/0 = 1.0" survives review (§5-A2): the number looks perfect
    and the document never says over how many obligations.
    """
    entry = (
        _assertion_strength(declared=None, surfaces=(), value=1.0)
        if key == "assertion_strength"
        else _entry(layer=next(iter(METRIC_LAYERS[key])), declared=None, value=1.0)
    )
    with pytest.raises(ValidationError):
        _document(metrics={key: entry})


@pytest.mark.parametrize("key", SCOPED_RATIO_KEYS)
def test_an_evaluated_coverage_ratio_publishes_its_tally(key: MetricKey) -> None:
    entry = (
        _assertion_strength()
        if key == "assertion_strength"
        else _entry(
            layer=next(iter(METRIC_LAYERS[key])), declared=MetricScope.of(total=4, covered=3), value=0.75
        )
    )
    document = _document(metrics={key: entry})
    published = document.metrics[key].declared
    assert published is not None
    assert published.total >= 1


@pytest.mark.parametrize("key", SCALAR_KEYS)
def test_a_scalar_metric_may_not_publish_a_denominator(key: MetricKey) -> None:
    """A scope on a scalar invites a floor over a set nobody defined; the card's
    detail (survivors, drift, slack) belongs in the evidence artifact."""
    with pytest.raises(ValidationError):
        _document(
            metrics={
                key: _entry(
                    layer=next(iter(METRIC_LAYERS[key])),
                    value=1.0,
                    declared=MetricScope.of(total=4, covered=4),
                    evidence=f"{key}.json",
                )
            }
        )


@pytest.mark.parametrize("key", SCALAR_KEYS)
def test_a_scalar_metric_publishes_one_number(key: MetricKey) -> None:
    document = _document(
        metrics={
            key: _entry(
                layer=next(iter(METRIC_LAYERS[key])), value=250.0, declared=None, evidence=f"{key}.json"
            )
        }
    )
    assert document.metrics[key].scopes == ()


@pytest.mark.parametrize("layer", ["api", "e2e", "fuzz"])
def test_the_discovery_metric_publishes_one_active_search_layer(layer: str) -> None:
    """§5-B3: yield follows the search layer, one at a time (api first).

    A breakdown is refused rather than encouraged: unlike assertion strength there
    is no second classifier to compare, so a multi-surface yield would be one
    number split by guesswork.
    """
    document = _document(
        metrics={
            "adversarial_yield": _entry(
                layer=layer, value=3.0, declared=None, evidence="adversarial-yield.json"
            )
        }
    )
    assert document.metrics["adversarial_yield"].layer == layer
    with pytest.raises(ValidationError):
        _document(
            metrics={
                "adversarial_yield": _entry(
                    layer=layer,
                    value=3.0,
                    declared=None,
                    surfaces=(_surface("api", 4, 2),),
                    evidence="adversarial-yield.json",
                )
            }
        )


# --------------------------------------------------------------------------- #
# a two-classifier metric publishes each surface independently (§5-B2)
# --------------------------------------------------------------------------- #


def test_a_multi_surface_metric_publishes_a_slice_per_surface() -> None:
    document = _document(metrics={"assertion_strength": _assertion_strength()})
    entry = document.metrics["assertion_strength"]
    assert [surface.layer for surface in entry.surfaces] == ["api", "e2e"]
    assert entry.value == 8 / 15
    assert entry.surfaces[0].value == 0.6
    assert entry.surfaces[1].value == 0.4


def test_an_evaluated_multi_surface_metric_may_not_omit_a_surface() -> None:
    """One measured classifier may not stand in for one nobody ran."""
    with pytest.raises(ValidationError):
        _document(
            metrics={
                "assertion_strength": _assertion_strength(
                    surfaces=(_surface("api", 10, 6),),
                    declared=MetricScope.of(total=10, covered=6),
                    value=0.6,
                )
            }
        )


def test_a_surface_outside_the_declared_set_is_refused() -> None:
    with pytest.raises(ValidationError):
        _document(
            metrics={
                "assertion_strength": _assertion_strength(
                    surfaces=(_surface("api", 10, 6), _surface("fuzz", 5, 2)),
                )
            }
        )


def test_each_surface_is_measured_in_its_own_right() -> None:
    """Every field of a slice is required: a listed surface was really measured."""
    for missing in ("value", "declared", "evidence"):
        payload = {
            "layer": "api",
            "value": 0.6,
            "declared": MetricScope.of(total=10, covered=6),
            "evidence": "assertion-strength-api.json",
        }
        del payload[missing]
        with pytest.raises(ValidationError):
            MetricSurface(**payload)


def test_an_aggregate_is_the_pooled_sum_of_its_surfaces_never_their_mean() -> None:
    """The check that makes "no averaging" mechanical.

    A producer that measured api at 0.6, guessed e2e at 0.4 and published the mean
    0.5 has two ways to go, and both are closed: keep the real pooled tally and the
    0.5 contradicts it, or invent a tally matching 0.5 and it no longer sums to the
    slices.
    """
    with pytest.raises(ValidationError):
        _assertion_strength(value=0.5)
    with pytest.raises(ValidationError):
        _assertion_strength(value=0.5, declared=MetricScope.of(total=4, covered=2))


def test_a_single_surface_metric_may_not_invent_a_breakdown() -> None:
    with pytest.raises(ValidationError):
        _document(metrics={"constraint_coverage": _entry(surfaces=(_surface("api", 4, 4),))})


def test_the_surfaces_are_sorted_at_the_boundary() -> None:
    """A producer's iteration order must not change the replayed bytes."""
    entry = _assertion_strength(surfaces=(_surface("e2e", 5, 2), _surface("api", 10, 6)))
    assert [surface.layer for surface in entry.surfaces] == ["api", "e2e"]


def test_a_touched_aggregate_needs_every_surface_to_state_its_part() -> None:
    with pytest.raises(ValidationError):
        _assertion_strength(touched=MetricScope.of(total=3, covered=2))


def test_a_touched_aggregate_is_the_pooled_sum_too() -> None:
    surfaces = (
        _surface("api", 10, 6, touched=MetricScope.of(total=2, covered=2)),
        _surface("e2e", 5, 2, touched=MetricScope.of(total=1, covered=0)),
    )
    entry = _assertion_strength(surfaces=surfaces, touched=MetricScope.of(total=3, covered=2))
    assert entry.touched is not None
    assert entry.touched.value == 2 / 3
    with pytest.raises(ValidationError):
        _assertion_strength(surfaces=surfaces, touched=MetricScope.of(total=3, covered=3))


def test_an_unevaluated_multi_surface_metric_carries_no_surfaces() -> None:
    """How a producer with only the api classifier reports: pending, not partial."""
    document = _document(
        metrics={
            "assertion_strength": _unmeasured(layer="cross"),
            "constraint_coverage": _entry(),
        },
        shortboards=(MetricShortboard(code="pending_nightly", metric="assertion_strength"),),
    )
    assert document.metrics["assertion_strength"].surfaces == ()
    with pytest.raises(ValidationError):
        _unmeasured(layer="cross", surfaces=(_surface("api", 10, 6),))


def test_a_floor_over_the_canonical_key_reads_the_pooled_ratio_not_a_surface() -> None:
    """Per-surface detail is detail: `floor_ratio` minimises over `value`, and
    `value` is the pooled population — so a floor on the canonical key cannot
    accidentally end up reading whichever surface happened to be listed first.
    A floor that wants one surface has to target it explicitly (Task 2)."""
    balanced = _assertion_strength(api=(10, 4), e2e=(5, 4))
    skewed = _assertion_strength(api=(5, 1), e2e=(10, 7))
    assert balanced.value == skewed.value == 8 / 15
    assert {surface.value for surface in balanced.surfaces} != {surface.value for surface in skewed.surfaces}


# --------------------------------------------------------------------------- #
# a scope is a tally; an empty denominator and an empty touched set differ
# --------------------------------------------------------------------------- #


def test_a_scope_derives_its_ratio_from_its_tally() -> None:
    scope = MetricScope.of(total=4, covered=3, uncovered=("entities.dept.constraints.name_unique",))
    assert scope.value == 0.75
    assert scope.uncovered == ("entities.dept.constraints.name_unique",)
    assert scope.open_slots == 1


def test_an_empty_set_has_no_value_rather_than_a_vacuous_one() -> None:
    """Spec §5-A2: 0/0 = 1.0 is the most hidden fail-open in the system."""
    assert MetricScope.of(total=0, covered=0).value is None


def test_a_scope_may_not_claim_a_ratio_over_an_empty_set() -> None:
    with pytest.raises(ValidationError):
        MetricScope(total=0, covered=0, value=1.0)


def test_a_scope_whose_ratio_contradicts_its_tally_is_refused() -> None:
    with pytest.raises(ValidationError):
        MetricScope(total=4, covered=2, value=1.0)


def test_a_scope_cannot_cover_more_than_its_denominator() -> None:
    with pytest.raises(ValidationError):
        MetricScope.of(total=2, covered=3)


def test_a_fully_covered_scope_cannot_list_uncovered_members() -> None:
    with pytest.raises(ValidationError):
        MetricScope.of(total=2, covered=2, uncovered=("dept.name_unique",))


def test_the_open_members_are_sorted_at_the_boundary() -> None:
    """Normalized on the way in, so the replay anchor does not depend on the order
    a producer walked its denominator."""
    scope = MetricScope.of(total=3, covered=0, uncovered=("c.key", "a.key", "b.key"))
    assert scope.uncovered == ("a.key", "b.key", "c.key")


def test_an_open_member_listed_twice_is_refused() -> None:
    """Deduplicating silently would hide a producer that double-counted."""
    with pytest.raises(ValidationError):
        MetricScope.of(total=3, covered=0, uncovered=("a.key", "a.key"))


def test_an_evaluated_entry_cannot_rest_on_an_empty_declared_denominator() -> None:
    """Nothing declared is a gap (`entity_without_constraints`), not a full score."""
    with pytest.raises(ValidationError):
        _entry(declared=MetricScope.of(total=0, covered=0), value=None)


def test_an_explicitly_empty_touched_set_is_representable() -> None:
    """ "This change touched nothing in the denominator" is a computed fact.

    It must be sayable, and it must not be sayable as 1.0: a touched-target floor
    reading `touched.value` gets `None` and has to decide what an empty subset
    means rather than being handed a vacuous pass.
    """
    document = _document(
        metrics={
            "constraint_coverage": _entry(
                declared=MetricScope.of(total=6, covered=6),
                touched=MetricScope.of(total=0, covered=0),
            )
        }
    )
    touched = document.metrics["constraint_coverage"].touched
    assert touched is not None
    assert touched.total == 0
    assert touched.value is None


def test_an_empty_touched_set_is_distinct_from_an_uncomputed_one() -> None:
    declared = MetricScope.of(total=6, covered=6)
    computed_empty = _entry(declared=declared, touched=MetricScope.of(total=0, covered=0))
    not_computed = _entry(declared=declared)
    assert computed_empty.touched is not None
    assert not_computed.touched is None
    assert computed_empty != not_computed


def test_touched_scope_is_an_entry_field_not_a_key() -> None:
    entry = _entry(
        declared=MetricScope.of(total=6, covered=3, uncovered=("a", "b", "c")),
        touched=MetricScope.of(total=2, covered=2),
        value=0.5,
    )
    assert entry.touched is not None
    assert entry.touched.value == 1.0
    assert "touched" in MetricEntry.model_fields


def test_a_touched_subset_needs_the_declared_scope_it_is_a_subset_of() -> None:
    with pytest.raises(ValidationError):
        _entry(declared=None, value=None, touched=MetricScope.of(total=2, covered=2))


def test_a_touched_subset_may_not_exceed_the_declared_total() -> None:
    with pytest.raises(ValidationError):
        _entry(
            declared=MetricScope.of(total=2, covered=2),
            touched=MetricScope.of(total=3, covered=3),
            value=1.0,
        )


def test_a_touched_subset_may_not_exceed_the_declared_covered_count() -> None:
    with pytest.raises(ValidationError):
        _entry(
            declared=MetricScope.of(total=4, covered=1),
            touched=MetricScope.of(total=2, covered=2),
            value=0.25,
        )


def test_a_touched_subset_may_not_leave_more_open_than_the_whole_denominator() -> None:
    """Declared 3/4 leaves one member open, so a subset cannot leave three.

    It is the shape a producer lands on by tallying the two scopes over different
    member sets, and the counts alone would not have caught it.
    """
    with pytest.raises(ValidationError):
        _entry(
            declared=MetricScope.of(total=4, covered=3),
            touched=MetricScope.of(total=3, covered=0),
            value=0.75,
        )


def test_a_numeric_value_restates_the_declared_scope_it_came_from() -> None:
    with pytest.raises(ValidationError):
        _entry(declared=MetricScope.of(total=4, covered=2), value=1.0)


# --------------------------------------------------------------------------- #
# an entry's status, its measurement and its evidence are one fact
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("status", ["not_evaluated", "skipped", "collection_failed"])
def test_an_unevaluated_entry_may_not_carry_a_measurement(status: str) -> None:
    with pytest.raises(ValidationError):
        _unmeasured(status=status, value=0.8)


@pytest.mark.parametrize("status", ["not_evaluated", "skipped", "collection_failed"])
def test_an_unevaluated_entry_may_not_carry_a_tally(status: str) -> None:
    with pytest.raises(ValidationError):
        _unmeasured(status=status, declared=MetricScope.of(total=2, covered=1))


def test_an_evaluated_entry_must_carry_exactly_one_measurement() -> None:
    with pytest.raises(ValidationError):
        _entry(value=None)
    with pytest.raises(ValidationError):
        _entry(value=1.0, holds=True)


def test_an_evaluated_entry_names_the_artifact_it_was_folded_from() -> None:
    """A measurement nothing can be traced back to is not evidence (§9)."""
    with pytest.raises(ValidationError):
        _entry(evidence="")


@pytest.mark.parametrize("status", ["not_evaluated", "skipped"])
def test_an_unevaluated_entry_needs_no_evidence_pointer(status: str) -> None:
    assert _unmeasured(status=status).evidence == ""


def test_a_boolean_entry_states_its_fact_apart_from_the_numeric_value() -> None:
    """Spec §7: boolean entries are deterministic checks and never enter the ratio."""
    entry = _entry(value=None, declared=None, holds=True, evidence="adversarial-yield.json")
    assert entry.holds is True
    assert entry.value is None


def test_a_boolean_entry_may_not_carry_a_tally() -> None:
    with pytest.raises(ValidationError):
        _entry(value=None, holds=True, declared=MetricScope.of(total=2, covered=1))


# --------------------------------------------------------------------------- #
# a whole-metric failure is named; a subject-scoped gap leaves the metric usable
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("code", sorted(WHOLE_METRIC_GAP_CODES))
def test_a_whole_metric_gap_carries_no_subject(code: MetricCollectionGapCode) -> None:
    with pytest.raises(ValidationError):
        MetricCollectionGap(code=code, metric="constraint_coverage", subject="entities.dept")


@pytest.mark.parametrize("code", sorted(SUBJECT_SCOPED_GAP_CODES))
def test_a_subject_scoped_gap_requires_its_subject(code: MetricCollectionGapCode) -> None:
    with pytest.raises(ValidationError):
        MetricCollectionGap(code=code, metric="constraint_coverage")


def test_identity_mismatch_is_sayable_at_either_scale() -> None:
    whole = MetricCollectionGap(code="identity_mismatch", metric="journey_coverage")
    scoped = MetricCollectionGap(
        code="identity_mismatch", metric="journey_coverage", subject="cases/TC_E2E_001"
    )
    assert whole.is_whole_metric
    assert not scoped.is_whole_metric


def test_a_failed_entry_must_be_named_by_a_whole_metric_gap() -> None:
    with pytest.raises(ValidationError):
        _document(metrics={"constraint_coverage": _unmeasured(status="collection_failed")})


def test_a_subject_scoped_gap_does_not_excuse_a_failed_entry() -> None:
    """A failed collection needs a *whole-metric* gap; naming one entity is not it."""
    with pytest.raises(ValidationError):
        _document(
            metrics={"constraint_coverage": _unmeasured(status="collection_failed")},
            collection_gaps=(
                MetricCollectionGap(
                    code="entity_without_constraints",
                    metric="constraint_coverage",
                    subject="entities.dept",
                ),
            ),
        )


def test_a_whole_metric_gap_may_not_sit_beside_a_measurement() -> None:
    with pytest.raises(ValidationError):
        _document(
            metrics={"constraint_coverage": _entry()},
            collection_gaps=(MetricCollectionGap(code="collection_failed", metric="constraint_coverage"),),
        )


def test_a_subject_scoped_gap_coexists_with_an_evaluated_partial_metric() -> None:
    """Spec §5-A2's ordinary case: five entities measured, a sixth undeclared.

    Refusing this pairing would force a producer to throw away sound measurements
    because one member of the denominator could not be judged — and the gap still
    routes the change to needs_human, so nothing is lost by keeping them.
    """
    document = _document(
        metrics={
            "constraint_coverage": _entry(
                declared=MetricScope.of(total=5, covered=5),
                touched=MetricScope.of(total=2, covered=2),
            )
        },
        collection_gaps=(
            MetricCollectionGap(
                code="entity_without_constraints",
                metric="constraint_coverage",
                subject="entities.audit_log",
                detail="touched entity declares no constraints",
            ),
        ),
    )
    assert document.metrics["constraint_coverage"].status == "evaluated"
    assert document.collection_gaps[0].subject == "entities.audit_log"


def test_a_property_marker_naming_an_unknown_key_is_a_subject_scoped_gap() -> None:
    document = _document(
        collection_gaps=(
            MetricCollectionGap(
                code="property_unknown_key",
                metric="constraint_coverage",
                subject="entities.dept.constraints.name_unqiue",
            ),
        )
    )
    assert not document.collection_gaps[0].is_whole_metric


def test_a_named_whole_metric_failure_is_constructible() -> None:
    document = _document(
        metrics={"constraint_coverage": _unmeasured(status="collection_failed")},
        collection_gaps=(
            MetricCollectionGap(
                code="collection_failed",
                metric="constraint_coverage",
                detail="compute-constraint-coverage raised",
            ),
        ),
    )
    assert document.collection_gaps[0].metric == "constraint_coverage"


def test_every_gap_names_a_metric_the_document_publishes() -> None:
    with pytest.raises(ValidationError):
        _document(
            collection_gaps=(
                MetricCollectionGap(
                    code="property_unknown_key", metric="journey_coverage", subject="j.unknown"
                ),
            )
        )


def test_a_gap_about_an_unknown_metric_key_is_refused() -> None:
    with pytest.raises(ValidationError):
        MetricCollectionGap(code="collection_failed", metric="vibes")  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# the published risk tier is checkable, and lowering is audited per case
# --------------------------------------------------------------------------- #


def test_the_risk_facts_are_all_required() -> None:
    """None of the five may default: a document that omitted the declaration or
    the audit would read as "nobody declared anything", which is a claim."""
    for name in (
        "risk_tier",
        "risk_tier_lower_bound",
        "risk_tier_declared",
        "risk_declaration_lowered",
        "risk_lowered_declarations",
    ):
        assert MetricsDocument.model_fields[name].is_required(), name


def test_a_document_may_not_publish_a_tier_below_its_mechanical_bound() -> None:
    """Spec §7 / v1 problem #6: the observed P0×medium downgrade must be unwritable."""
    with pytest.raises(ValidationError):
        _document(risk_tier="medium", risk_tier_lower_bound="high")


def test_a_document_may_not_ignore_a_higher_declaration() -> None:
    with pytest.raises(ValidationError):
        _document(
            risk_tier="medium",
            risk_tier_lower_bound="medium",
            risk_tier_declared="critical",
        )


def test_a_document_publishes_the_declaration_that_raised_the_band() -> None:
    document = _document(risk_tier="critical", risk_tier_lower_bound="medium", risk_tier_declared="critical")
    assert document.risk_tier == "critical"
    assert document.risk_declaration_lowered is False


def test_the_lowering_flag_must_match_its_audit() -> None:
    lowered = RiskDeclarationLowered(case_id="TC_API_001", declared="medium", lower_bound="high")
    with pytest.raises(ValidationError):
        _document(
            risk_tier="high",
            risk_tier_lower_bound="high",
            risk_tier_declared="medium",
            risk_declaration_lowered=False,
            risk_lowered_declarations=(lowered,),
        )
    with pytest.raises(ValidationError):
        _document(risk_declaration_lowered=True, risk_lowered_declarations=())


def test_one_case_declaring_critical_cannot_hide_another_declaring_low() -> None:
    """The reason the audit is per case (§2.3's authoring behaviour).

    `max(declared)` is `critical` and above the bound, so an aggregate comparison
    reports nothing lowered while a P0 case asking for `low` sits in the same
    change. The audit names it, and the flag follows the audit.
    """
    document = _document(
        risk_tier="critical",
        risk_tier_lower_bound="high",
        risk_tier_declared="critical",
        risk_declaration_lowered=True,
        risk_lowered_declarations=(
            RiskDeclarationLowered(case_id="TC_API_001", declared="low", lower_bound="high"),
        ),
    )
    assert document.risk_tier == "critical"
    assert document.risk_declaration_lowered is True
    assert document.risk_lowered_declarations[0].declared == "low"


def test_an_audited_case_must_have_declared_below_its_own_bound() -> None:
    with pytest.raises(ValidationError):
        RiskDeclarationLowered(case_id="TC_API_001", declared="high", lower_bound="high")


def test_an_audited_case_cannot_claim_a_bound_above_the_change_s() -> None:
    """The change's bound is the max over exactly these per-case bounds."""
    with pytest.raises(ValidationError):
        _document(
            risk_tier="medium",
            risk_tier_lower_bound="medium",
            risk_declaration_lowered=True,
            risk_lowered_declarations=(
                RiskDeclarationLowered(case_id="TC_API_001", declared="medium", lower_bound="critical"),
            ),
        )


def test_the_audit_is_sorted_and_names_each_case_once() -> None:
    first = RiskDeclarationLowered(case_id="TC_API_002", declared="low", lower_bound="medium")
    second = RiskDeclarationLowered(case_id="TC_API_009", declared="low", lower_bound="medium")
    document = _document(risk_declaration_lowered=True, risk_lowered_declarations=(second, first))
    assert [lowered.case_id for lowered in document.risk_lowered_declarations] == [
        "TC_API_002",
        "TC_API_009",
    ]
    with pytest.raises(ValidationError):
        _document(risk_declaration_lowered=True, risk_lowered_declarations=(first, first))


def test_the_factory_publishes_all_five_risk_facts_from_one_resolution() -> None:
    document = MetricsDocument.of(
        risk=_Risk(tier="critical", lower_bound="medium", declared="critical", declaration_lowered=False),
        change_id="CH-API-001",
        cadence="pr",
        computed_at=COMPUTED_AT,
        metrics={"constraint_coverage": _entry()},
        policy_digest=DIGEST,
    )
    assert document.schema_version == "2"
    assert (document.risk_tier, document.risk_tier_lower_bound) == ("critical", "medium")
    assert document.risk_tier_declared == "critical"
    assert document.risk_declaration_lowered is False
    assert document.risk_lowered_declarations == ()


def test_the_factory_preserves_a_p0_case_labelled_medium_as_high() -> None:
    """The pinned regression, at the publication boundary."""
    document = MetricsDocument.of(
        risk=_Risk(
            tier="high",
            lower_bound="high",
            declared="medium",
            declaration_lowered=True,
            lowered_declarations=(
                RiskDeclarationLowered(case_id="TC_API_001", declared="medium", lower_bound="high"),
            ),
        ),
        change_id="CH-API-001",
        cadence="pr",
        computed_at=COMPUTED_AT,
        metrics={"constraint_coverage": _entry()},
        policy_digest=DIGEST,
    )
    assert document.risk_tier == "high"
    assert document.risk_tier_declared == "medium"
    assert document.risk_declaration_lowered is True
    assert document.risk_lowered_declarations[0].case_id == "TC_API_001"


def test_the_factory_refuses_a_resolution_that_does_not_add_up() -> None:
    """The producer hands over one resolution; a hand-built one is still checked."""
    with pytest.raises(ValidationError):
        MetricsDocument.of(
            risk=_Risk(tier="medium", lower_bound="high", declared="medium", declaration_lowered=True),
            change_id="CH-API-001",
            cadence="pr",
            computed_at=COMPUTED_AT,
            metrics={"constraint_coverage": _entry()},
            policy_digest=DIGEST,
        )


def test_the_tier_vocabulary_is_ordered_by_its_declaration() -> None:
    assert RISK_TIER_ORDER == get_args(RiskTier)
    assert RISK_TIER_ORDER == ("low", "medium", "high", "critical")


# --------------------------------------------------------------------------- #
# shortboards and floor_ratio
# --------------------------------------------------------------------------- #


def test_a_shortboard_must_name_a_metric_the_document_publishes() -> None:
    with pytest.raises(ValidationError):
        _document(shortboards=(MetricShortboard(code="below_floor", metric="journey_coverage"),))


def test_a_shortboard_names_a_published_metric() -> None:
    document = _document(
        shortboards=(MetricShortboard(code="below_floor", metric="constraint_coverage", detail="0.5 < 0.7"),)
    )
    assert document.shortboards[0].metric == "constraint_coverage"


def test_a_pending_nightly_metric_is_not_evaluated_rather_than_zero() -> None:
    document = _document(
        metrics={
            "constraint_coverage": _entry(),
            "mutation_score": _unmeasured(layer="backend"),
        },
        shortboards=(MetricShortboard(code="pending_nightly", metric="mutation_score"),),
    )
    assert document.metrics["mutation_score"].value is None


def test_floor_ratio_needs_an_evaluated_numeric_metric_to_be_the_minimum_of() -> None:
    with pytest.raises(ValidationError):
        _document(
            metrics={
                "adversarial_clean": _entry(
                    layer="cross",
                    value=None,
                    declared=None,
                    holds=True,
                    evidence="adversarial-yield.json",
                )
            },
            floor_ratio=1.0,
        )


def test_floor_ratio_is_a_bounded_achievement_ratio() -> None:
    assert _document(floor_ratio=0.5).floor_ratio == 0.5
    with pytest.raises(ValidationError):
        _document(floor_ratio=1.5)


def test_the_document_never_names_the_ratio_a_confidence() -> None:
    """Spec §3.10/§12.14: an unfit-for-purpose reading of the vector is barred by name."""
    for model in (MetricsDocument, MetricEntry, MetricSurface, MetricScope, MetricShortboard):
        assert not any("confidence" in name for name in model.model_fields)


def test_the_document_carries_no_route() -> None:
    """Routing is `metrics-sufficiency-gate`'s job (spec §6); the producer states facts."""
    forbidden = {"verdict", "route", "disposition", "final_status", "quality_gate"}
    assert forbidden.isdisjoint(MetricsDocument.model_fields)


# --------------------------------------------------------------------------- #
# wire format, identity and immutability
# --------------------------------------------------------------------------- #


def test_an_unknown_field_is_refused() -> None:
    with pytest.raises(ValidationError):
        _document(confidence=0.9)


def test_the_document_is_immutable() -> None:
    document = _document()
    with pytest.raises(ValidationError):
        document.risk_tier = "high"  # type: ignore[misc]


def test_the_metric_map_cannot_be_edited_in_place() -> None:
    """`frozen=True` only stops rebinding the field; a mutable map would leave a
    validated document editable through it."""
    document = _document()
    with pytest.raises(TypeError):
        document.metrics["journey_coverage"] = _entry(layer="e2e")  # type: ignore[index]
    with pytest.raises(TypeError):
        del document.metrics["constraint_coverage"]  # type: ignore[attr-defined]


def test_a_published_entry_cannot_be_mutated_through_the_map() -> None:
    document = _document()
    with pytest.raises(ValidationError):
        document.metrics["constraint_coverage"].value = 0.1  # type: ignore[misc]


def test_the_metric_map_is_a_copy_of_the_callers_mapping() -> None:
    source: dict[str, Any] = {"constraint_coverage": _entry()}
    document = _document(metrics=source)
    source["journey_coverage"] = _entry(layer="e2e", evidence="journey-coverage.json")
    assert set(document.metrics) == {"constraint_coverage"}


def test_a_naive_computed_at_is_refused() -> None:
    with pytest.raises(ValidationError):
        _document(computed_at=datetime(2026, 8, 4, 9, 30))


def test_an_empty_change_id_is_refused() -> None:
    with pytest.raises(ValidationError):
        _document(change_id="")


@pytest.mark.parametrize("digest", ["", "not-a-digest", "D" * 64, "d" * 63, f"sha256:{'d' * 64}"])
def test_a_policy_digest_that_is_not_a_bare_sha256_hex_is_refused(digest: str) -> None:
    """`artifacts.policy.policy_digest` returns bare lowercase hex; a placeholder
    here would make the document's "judged under which policy" unanswerable."""
    with pytest.raises(ValidationError):
        _document(policy_digest=digest)


def test_a_nightly_document_is_representable() -> None:
    """§9 keeps nightly metrics on their own path so the PR document is written
    once; the cadence they were collected under travels in the document."""
    assert _document(cadence="nightly").cadence == "nightly"


def test_cadence_artifact_paths_are_closed_and_distinct() -> None:
    """PR and nightly share MetricsDocument but never the same registered path."""
    assert set(ARTIFACT_REL_BY_CADENCE) == set(get_args(MetricCadence))
    assert ARTIFACT_REL_BY_CADENCE["pr"] == PR_METRICS_REL == "inspect/metrics.json"
    assert ARTIFACT_REL_BY_CADENCE["nightly"] == NIGHTLY_METRICS_REL == "inspect/metrics-nightly.json"
    assert PR_METRICS_REL != NIGHTLY_METRICS_REL


# --------------------------------------------------------------------------- #
# replay anchor
# --------------------------------------------------------------------------- #


def test_the_replay_anchor_excludes_the_clock_and_sorts_the_metrics() -> None:
    """Spec §9: same inputs replay to the same bytes; `computed_at` is injected."""
    journey = _entry(layer="e2e", evidence="journey-coverage.json")
    first = _document(metrics={"journey_coverage": journey, "constraint_coverage": _entry()})
    later = _document(
        metrics={"constraint_coverage": _entry(), "journey_coverage": journey},
        computed_at=datetime(2026, 8, 5, 1, 2, tzinfo=UTC),
    )
    assert first.replay_subtree() == later.replay_subtree()
    assert list(first.replay_subtree()["metrics"]) == ["constraint_coverage", "journey_coverage"]
    assert "computed_at" not in first.replay_subtree()


def test_the_replay_anchor_sorts_gaps_and_shortboards() -> None:
    """A producer's append order must not change the bytes a replay compares."""
    gaps = (
        MetricCollectionGap(code="property_unknown_key", metric="constraint_coverage", subject="b.key"),
        MetricCollectionGap(
            code="entity_without_constraints", metric="constraint_coverage", subject="a.entity"
        ),
    )
    boards = (
        MetricShortboard(code="pending_nightly", metric="constraint_coverage"),
        MetricShortboard(code="below_floor", metric="constraint_coverage"),
    )
    forward = _document(collection_gaps=gaps, shortboards=boards)
    reversed_ = _document(collection_gaps=gaps[::-1], shortboards=boards[::-1])
    assert forward.replay_subtree() == reversed_.replay_subtree()
    anchor = forward.replay_subtree()
    assert [gap["code"] for gap in anchor["collection_gaps"]] == [
        "entity_without_constraints",
        "property_unknown_key",
    ]
    assert [board["code"] for board in anchor["shortboards"]] == ["below_floor", "pending_nightly"]


def test_the_replay_anchor_carries_the_surface_breakdown_in_a_stable_order() -> None:
    """Per-surface detail is part of the replayed facts, and it replays identically
    whichever order the two classifiers reported in."""
    forward = _document(metrics={"assertion_strength": _assertion_strength()})
    reversed_ = _document(
        metrics={
            "assertion_strength": _assertion_strength(
                surfaces=(_surface("e2e", 5, 2), _surface("api", 10, 6))
            )
        }
    )
    anchor = forward.replay_subtree()
    assert anchor == reversed_.replay_subtree()
    assert [surface["layer"] for surface in anchor["metrics"]["assertion_strength"]["surfaces"]] == [
        "api",
        "e2e",
    ]


def test_the_replay_anchor_normalizes_the_open_members() -> None:
    forward = _document(
        metrics={
            "constraint_coverage": _entry(
                declared=MetricScope.of(total=4, covered=1, uncovered=("c", "a", "b")), value=0.25
            )
        }
    )
    reversed_ = _document(
        metrics={
            "constraint_coverage": _entry(
                declared=MetricScope.of(total=4, covered=1, uncovered=("b", "c", "a")), value=0.25
            )
        }
    )
    assert forward.replay_subtree() == reversed_.replay_subtree()
    assert forward.replay_subtree()["metrics"]["constraint_coverage"]["declared"]["uncovered"] == [
        "a",
        "b",
        "c",
    ]


def test_the_replay_anchor_tracks_a_changed_measurement() -> None:
    changed = _document(
        metrics={"constraint_coverage": _entry(value=0.5, declared=MetricScope.of(total=4, covered=2))}
    )
    assert changed.replay_subtree() != _document().replay_subtree()


def test_the_replay_anchor_tracks_a_changed_surface_split() -> None:
    """Same pooled aggregate, different populations: the anchor still notices."""
    balanced = _document(metrics={"assertion_strength": _assertion_strength(api=(10, 4), e2e=(5, 4))})
    skewed = _document(metrics={"assertion_strength": _assertion_strength(api=(5, 1), e2e=(10, 7))})
    assert balanced.replay_subtree() != skewed.replay_subtree()


def test_the_json_dump_round_trips() -> None:
    document = _document(
        metrics={
            "constraint_coverage": _entry(
                declared=MetricScope.of(total=6, covered=5, uncovered=("entities.dept.name_unique",)),
                touched=MetricScope.of(total=0, covered=0),
                value=5 / 6,
            )
        },
        shortboards=(MetricShortboard(code="below_floor", metric="constraint_coverage"),),
        floor_ratio=5 / 6,
    )
    dumped = document.model_dump(mode="json")
    assert dumped["metrics"]["constraint_coverage"]["touched"]["total"] == 0
    assert MetricsDocument.model_validate(dumped) == document


def test_the_json_dump_round_trips_a_multi_surface_metric_and_its_audit() -> None:
    document = _document(
        risk_tier="high",
        risk_tier_lower_bound="high",
        risk_tier_declared="medium",
        risk_declaration_lowered=True,
        risk_lowered_declarations=(
            RiskDeclarationLowered(case_id="TC_API_001", declared="medium", lower_bound="high"),
        ),
        metrics={
            "assertion_strength": _assertion_strength(
                surfaces=(
                    _surface("api", 10, 6, touched=MetricScope.of(total=2, covered=2)),
                    _surface("e2e", 5, 2, touched=MetricScope.of(total=1, covered=0)),
                ),
                touched=MetricScope.of(total=3, covered=2),
            )
        },
    )
    dumped = document.model_dump(mode="json")
    surfaces = dumped["metrics"]["assertion_strength"]["surfaces"]
    assert [surface["layer"] for surface in surfaces] == ["api", "e2e"]
    assert dumped["risk_lowered_declarations"][0]["case_id"] == "TC_API_001"
    assert MetricsDocument.model_validate(dumped) == document
