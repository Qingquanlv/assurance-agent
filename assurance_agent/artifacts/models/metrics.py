"""inspect/metrics.json (must_compat): the verification metric vector.

What `aggregate-metrics` publishes and what `metrics-sufficiency-gate` reads
(design `docs/superpowers/specs/2026-08-03-verification-metrics-evidence-
sufficiency-design.md` §9). Like `trace_sufficiency.py`, the document carries
**facts and never a route**: the producer states what each metric amounts to, and
the gate's DSL maps those facts onto `pass`/`needs_human_review`/`stop`. A
`verdict` field here would put one routing table in the producer and a second in
the gate, and the two would drift.

What the model enforces, rather than leaving to a producer's discipline:

- **One closed key vocabulary, and each key declares the shape of its answer.**
  `MetricKey` is the single enumeration that `policy.evidence_sufficiency.floors`,
  the cadence lists and this artifact all draw from (§9, guard §12.7). It
  deliberately has no `*_touched` variants: keying scope into the name is a
  *second* vocabulary, and the two key sets promptly failed to close (v1 problem
  #7). Scope is a property of a measurement, so it is an entry field (`declared` /
  `touched`) and a floor names the canonical key plus the target scope it judges.
  `METRIC_SHAPES` then closes the way an entry could still lie about itself: a
  `scoped_ratio` key must publish the tally its ratio came from (a bare `1.0` for
  `constraint_coverage` is unauditable and is exactly how "0/0 = 1.0" gets past a
  reviewer), a `scalar` key must publish no denominator at all, and a `boolean`
  key answers with `holds` and never with `value`.
- **A multi-surface metric publishes each surface independently.** §5-B2 defines
  *two* assertion-strength classifiers, api and e2e, and a single number for both
  would let one measured surface stand in for one nobody ran. So an evaluated
  `assertion_strength` carries a `MetricSurface` per surface in
  `METRIC_SURFACES`, each with its own tally and evidence, and the entry-level
  tally must be the **pooled sum** of them — an average of ratios cannot satisfy
  that, which is the point. The per-surface breakdown is detail: a floor over the
  canonical key reads the pooled `value`, and a floor that wants one surface has
  to target it explicitly.
- **Two kinds of collection gap, distinguished by `subject`.** A gap with no
  subject is the *whole* metric failing, so the entry must say
  `collection_failed` and measure nothing. A subject-scoped gap
  (`entity_without_constraints`, `property_unknown_key`, a subject-level identity
  mismatch) names one member of the denominator and therefore *coexists* with an
  evaluated partial metric — refusing that pairing would have forced a producer
  to throw away five sound measurements because a sixth entity was undeclared.
  Either way the gap must name a metric this document publishes, or the operator
  it is addressed to has no entry to act on.
- **An empty denominator is not a measurement, but an empty touched set is.**
  `0/0 = 1.0` is the most hidden fail-open available here (§5-A2), so a scope with
  `total=0` has no value at all and an evaluated entry may not rest on an empty
  *declared* scope. An empty *touched* scope is different and legitimate: it says
  the change touched nothing in this denominator, which is a computed fact, and it
  is distinct from `touched=None`, which says nobody computed it.
- **The published risk tier is checkable, and lowering is audited per case.**
  `risk_tier`, `risk_tier_lower_bound`, `risk_tier_declared`,
  `risk_declaration_lowered` and `risk_lowered_declarations` are all published and
  cross-checked: the tier must be `max(lower_bound, declared)`, and the flag must
  be exactly "the audit list is non-empty" (§7). The audit list is per case
  because the aggregate hides the signal it exists for — one P0 case declaring
  `low` beside another declaring `critical` leaves the *maximum* declaration above
  the bound while the underselling case goes unrecorded.

**Producer handoff.** This document cannot compute a mechanical lower bound: it
has no cases. Producers resolve the band once, with
`evidence.risk_tier.resolve_risk_tier`, and hand the result to
`MetricsDocument.of`, which is the only place the five risk fields are derived
rather than retyped. The validators below still check a hand-built document, but
checking is second best.

`floor_ratio` is the floor achievement *minimum*, used only for the
`pass ↔ needs_human` decision. It is not a probability of correctness, and it is
never published under another name — `confidence` above all (§3.10, §12.14).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import datetime
from types import MappingProxyType
from typing import Annotated, Any, Literal, Protocol, Self, get_args, runtime_checkable

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    field_validator,
    model_validator,
)

from assurance_agent.artifacts.models.common import (
    RISK_TIER_ORDER,
    CaseId,
    NonEmptyStr,
    RiskTier,
)

_FROZEN = ConfigDict(frozen=True, extra="forbid")

# The one closed metric vocabulary (§9). Canonical names only: scope lives in the
# entry (see module docstring), never in a key variant.
MetricKey = Literal[
    # A layer — was the ground covered
    "diff_coverage",
    "constraint_coverage",
    "auth_matrix_coverage",
    "journey_coverage",
    "baseline_drift",
    # B layer — is the covering oracle real
    "mutation_score",
    "assertion_strength",
    "threshold_slack",
    "adversarial_yield",
    "adversarial_clean",
]
METRIC_KEYS: tuple[MetricKey, ...] = get_args(MetricKey)

# How a metric answers:
#   scoped_ratio — covered/total over an enumerable declared denominator, so the
#                  entry publishes the tally and the open members with the ratio
#   scalar       — one number with no denominator of obligations (a drift, a slack
#                  factor, a rate); whatever detail it has lives in its evidence
#                  artifact, which is how §9's own example publishes them
#   boolean      — a deterministic check, which §7 keeps out of `floor_ratio`
MetricShape = Literal["scoped_ratio", "scalar", "boolean"]

# Which field the answer goes in. Derived from the shape below rather than
# declared a second time, so the two cannot disagree.
MetricKind = Literal["numeric", "boolean"]

# Which verification surface a metric speaks about (§3.1: E2E and performance own
# metric families rather than being spoken for by API-layer numbers). `fuzz` is
# here because §5-B3 names it as one of the search layers counterexample yield
# follows. `cross` is for metrics that span surfaces by definition, not a default
# for "unsure".
MetricLayer = Literal["api", "e2e", "fuzz", "performance", "backend", "cross"]

# The shape each key answers in, from the §5 cards and §9's example document.
# Total over `MetricKey` and consulted by `MetricsDocument`.
#
# The three coverage ratios (§5-A2/A3/A4) are `scoped_ratio` because each is
# defined as a proportion of an enumerable closed key set — declared constraints,
# `endpoint × method × role` cells, declared journeys — and because every consumer
# that acts on one needs the open members, not the ratio (§9 publishes
# `total`/`covered`/`uncovered` for exactly these three). `assertion_strength` is
# a ratio too (§5-B2: the share of behaviour/state/contract assertions), pooled
# over its per-surface slices.
#
# The scalars are scalars for a reason each card states: `diff_coverage` is
# context that never gates and whose line detail lives in `coverage-diff.json`
# (§5-A1); `baseline_drift` is a regression magnitude against a batch baseline,
# not a proportion of anything (§5-A5); `mutation_score` is budget-capped
# sampling whose survivor list goes to the report and retro rather than into a
# floor (§5-B1); `threshold_slack` is `threshold / measured`, unbounded and not a
# ratio of covered things (§5-B5); `adversarial_yield` counts counterexamples per
# search round (§5-B3). Giving any of them a `declared` denominator would invite a
# floor over a denominator nobody defined.
METRIC_SHAPES: Mapping[MetricKey, MetricShape] = MappingProxyType(
    {
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
)

_KIND_BY_SHAPE: Mapping[MetricShape, MetricKind] = MappingProxyType(
    {"scoped_ratio": "numeric", "scalar": "numeric", "boolean": "boolean"}
)
METRIC_KINDS: Mapping[MetricKey, MetricKind] = MappingProxyType(
    {key: _KIND_BY_SHAPE[shape] for key, shape in METRIC_SHAPES.items()}
)

# The surface an *entry* may claim, also from the §5 cards. Single-layer for most.
# `assertion_strength` claims `cross` because the published entry is the pooled
# aggregate of its two classifiers (see `METRIC_SURFACES`), and
# `adversarial_yield` may claim any of the three search layers because §5-B3 says
# it follows whichever layer produced the counterexamples — one active layer at a
# time, api first, then e2e and fuzz, an ordering that is a rollout constraint
# (§5-B3, §14 M3) rather than something a document can violate.
METRIC_LAYERS: Mapping[MetricKey, frozenset[MetricLayer]] = MappingProxyType(
    {
        "diff_coverage": frozenset({"backend"}),
        "constraint_coverage": frozenset({"api"}),
        "auth_matrix_coverage": frozenset({"api"}),
        "journey_coverage": frozenset({"e2e"}),
        "baseline_drift": frozenset({"performance"}),
        "mutation_score": frozenset({"backend"}),
        "assertion_strength": frozenset({"cross"}),
        "threshold_slack": frozenset({"performance"}),
        "adversarial_yield": frozenset({"api", "e2e", "fuzz"}),
        "adversarial_clean": frozenset({"cross"}),
    }
)

# The surfaces a key must break its measurement down into. Empty for every metric
# that is collected on one surface; `{api, e2e}` for `assertion_strength`, whose
# §5-B2 card defines one classifier per surface. Non-empty here means an evaluated
# entry publishes *every* listed surface independently: a metric that spans
# surfaces and reports one number lets a measured surface stand in for an
# unmeasured one, which is the substitution §3.1 was written against. A producer
# with only the api classifier reports `not_evaluated` (plus a shortboard), not a
# half-measured aggregate.
METRIC_SURFACES: Mapping[MetricKey, frozenset[MetricLayer]] = MappingProxyType(
    {key: frozenset({"api", "e2e"}) if key == "assertion_strength" else frozenset() for key in METRIC_KEYS}
)

# Which cadence produced the document (§8). PR-cadence metrics land in
# `inspect/metrics.json`, nightly ones in `inspect/metrics-nightly.json` — a
# separate path and a separate registry entry (M2), so a second write never
# collides with the registry's immutability rule (§9).
MetricCadence = Literal["pr", "nightly"]

# Closed path map for the two registered MetricsDocument carriers. Nightly must
# never share the PR path: that is what keeps a passed PR verdict write-once.
PR_METRICS_REL = "inspect/metrics.json"
NIGHTLY_METRICS_REL = "inspect/metrics-nightly.json"
ARTIFACT_REL_BY_CADENCE: Mapping[MetricCadence, str] = MappingProxyType(
    {
        "pr": PR_METRICS_REL,
        "nightly": NIGHTLY_METRICS_REL,
    }
)

# Four states, because §6 needs all four to route:
#   evaluated         — measured this run; the only state carrying a measurement
#   not_evaluated     — value pending (nightly not yet backfilled): a shortboard,
#                       not a gap, and it does not enter `floor_ratio` (§7)
#   skipped           — this cadence legitimately does not collect it; no route
#   collection_failed — the operation failed or its artifact was corrupt: a typed
#                       gap and a needs_human route, never folded into `skipped`
MetricStatus = Literal["evaluated", "not_evaluated", "skipped", "collection_failed"]

# Typed collection gaps (§6/§10); every one routes to needs_human. A
# `not_evaluated` metric is deliberately not among them, because a legally
# pending value is not a gap. `mutation_budget_exceeded` is likewise absent:
# §5-B1 says exceeding the budget is not a failure, so it is a shortboard.
MetricCollectionGapCode = Literal[
    "collection_failed",
    "artifact_corrupt",
    "identity_mismatch",
    "entity_without_constraints",
    "property_unknown_key",
]

# Codes that can only mean "the whole metric failed": the operation raised, or
# its artifact could not be read at all. They carry no subject, and the entry they
# name must say `collection_failed`.
WHOLE_METRIC_GAP_CODES: frozenset[MetricCollectionGapCode] = frozenset(
    {"collection_failed", "artifact_corrupt"}
)
# Codes that are about one member of a denominator — an entity with no declared
# constraints, a marker naming a key that does not exist. They require a subject
# and leave the rest of the metric measurable.
SUBJECT_SCOPED_GAP_CODES: frozenset[MetricCollectionGapCode] = frozenset(
    {"entity_without_constraints", "property_unknown_key"}
)
# `identity_mismatch` is in neither set on purpose: an artifact whose identity
# does not match the manifest sinks the whole metric, while a single row naming a
# foreign batch sinks only that subject, and the producer says which by whether it
# can name one.

# Shortboards are the "shortest plank" list a warn-band floor produces (§6) plus
# the visibility signals that must never fail a batch on their own (§5-A5/B3/B5
# and §7's pending_nightly).
MetricShortboardCode = Literal[
    "below_floor",
    "pending_nightly",
    "threshold_slack_out_of_band",
    "baseline_drift_out_of_band",
    "sample_insufficient",
    "mutation_budget_exceeded",
    # §5-B3 / M3 Task 2: evaluated yield with unclosed_count > 0 (report + hard rule).
    "adversarial_open",
    # §5-C3 / M3 Task 4: quarantined key excluded from covered but kept in denominator.
    "quarantined_excluded",
]

# min(actual / floor, 1.0) over the evaluated numeric metrics, so the closed unit
# interval is the whole range a well-formed value can take.
FloorRatio = Annotated[float, Field(ge=0.0, le=1.0)]

# `artifacts.policy.policy_digest` returns a bare lowercase sha256 hex digest (the
# `sha256:`-prefixed convention elsewhere in this package is for content-addressed
# payload digests). Pinned so a document cannot record a placeholder policy.
_POLICY_DIGEST = r"^[0-9a-f]{64}$"


class MetricScope(BaseModel):
    """One denominator's tally, and the ratio it implies.

    Carried as a tally rather than a bare ratio because every consumer that acts
    on a coverage number needs the counts (§9's `total`/`covered`/`uncovered`):
    "0.83" cannot tell an operator whether one obligation is open or fifty.

    ``value`` is stored and checked rather than derived, so a document whose ratio
    disagrees with its counts is refused instead of silently recomputed into
    agreement — a producer that published the wrong number was wrong about
    something, and repairing it here would hide that from the evidence too.

    ``total=0`` is representable and means the set is *empty as computed*: for a
    ``touched`` scope that is the ordinary "this change touched nothing here", and
    it is deliberately distinguishable from a scope that is absent altogether. It
    has no ``value``, because the only ratio available over an empty set is the
    vacuous 1.0 that §5-A2 exists to refuse.
    """

    model_config = _FROZEN

    total: int = Field(ge=0)
    covered: int = Field(ge=0)
    # None exactly when `total` is 0.
    value: float | None
    # The open members, named so a gap is actionable. May be elided (a large set
    # need not be listed in full), never longer than the open slots. Sorted and
    # duplicate-free by construction — see `_normalize_the_open_members`.
    uncovered: tuple[str, ...] = ()

    @classmethod
    def of(cls, *, total: int, covered: int, uncovered: tuple[str, ...] = ()) -> MetricScope:
        """Build a scope carrying the ratio its counts imply.

        The only way a producer should construct one: dividing at each call site
        is how a "1.0" for an empty denominator gets written in the first place.
        """
        return cls(
            total=total,
            covered=covered,
            value=None if total == 0 else covered / total,
            uncovered=uncovered,
        )

    @field_validator("uncovered", mode="after")
    @classmethod
    def _normalize_the_open_members(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Sort here, at the boundary, rather than in each consumer.

        The replay anchor compares serialized documents (§9), so a producer that
        walked its denominator in a different order would replay to different
        bytes for the same facts. Normalizing on the way in means every copy of
        the scope — the document, the anchor, a report reading the field — sees
        the same order. Duplicates are refused rather than collapsed: a member
        listed twice makes the list disagree with the counts, and silently
        deduplicating would hide a producer that double-counted.
        """
        if len(set(value)) != len(value):
            raise ValueError("uncovered lists a member twice, so it disagrees with the counts")
        return tuple(sorted(value))

    @model_validator(mode="after")
    def _the_tally_and_the_ratio_agree(self) -> Self:
        if self.covered > self.total:
            raise ValueError(f"covered={self.covered} exceeds total={self.total}")
        if self.total == 0:
            if self.value is not None:
                raise ValueError("a scope with total=0 has no value: an empty set is not 1.0")
            return self
        expected = self.covered / self.total
        if self.value is None or not math.isclose(self.value, expected, rel_tol=1e-9, abs_tol=1e-9):
            raise ValueError(f"value={self.value} contradicts {self.covered}/{self.total}")
        return self

    @property
    def open_slots(self) -> int:
        """The members still to be covered."""
        return self.total - self.covered

    @model_validator(mode="after")
    def _a_covered_slot_cannot_also_be_listed_as_open(self) -> Self:
        if len(self.uncovered) > self.open_slots:
            raise ValueError(
                f"{len(self.uncovered)} uncovered members exceed the {self.open_slots} open slots "
                f"of {self.covered}/{self.total}"
            )
        return self


def _check_measurement(
    *, value: float | None, declared: MetricScope | None, touched: MetricScope | None
) -> None:
    """The scope arithmetic every measurement obeys, aggregate or per-surface slice.

    Shared as a function rather than a base class so each model can declare its own
    fields at its own strictness (a slice's tally is mandatory, an entry's is not)
    while the arithmetic stays stated once. Checked on slices as well as on the
    aggregate because pooled sums can add up perfectly while a slice carries a
    ratio that contradicts its own tally.

    Three invariants, each closing a specific fail-open:

    1. **An empty declared denominator is not a measurement** (§5-A2). Refused
       rather than reported as 0.0 or 1.0: 1.0 is the vacuous success the design
       exists to refuse, and 0.0 reads as "measured and terrible" when the truth is
       "there was nothing to measure". The producer emits an
       ``entity_without_constraints`` gap instead. Only the *declared* scope is
       judged — an empty ``touched`` subset is a fact about the change, not a hole
       in the obligations.
    2. **A touched subset is a subset, and has something to be a subset of.** All
       three counts are checked, including the open one: a subset with more open
       members than the whole denominator has (declared 3/4, touched 0/3) is
       arithmetic no operator can act on, and it is the shape a producer lands on
       by tallying the two scopes over different member sets. A ``touched`` with no
       ``declared`` is refused too, or a touched-target floor would compare against
       a denominator the document never stated.
    3. **The scalar restates the declared scope.** Pinning which scope ``value``
       speaks for is what lets a floor be declared against a plain metric key: the
       scalar is the declared scope, and a touched-target floor reads
       ``touched.value``. Without this, "0.6" could mean either scope and every
       consumer would guess.
    """
    if declared is not None and declared.total == 0:
        raise ValueError(
            "an empty declared denominator is a collection gap, not a metric: "
            "nothing was declared to measure against"
        )
    if touched is not None:
        if declared is None:
            raise ValueError("a touched subset needs the declared scope it is a subset of")
        if touched.total > declared.total:
            raise ValueError(f"touched total {touched.total} exceeds declared total {declared.total}")
        if touched.covered > declared.covered:
            raise ValueError(f"touched covered {touched.covered} exceeds declared covered {declared.covered}")
        if touched.open_slots > declared.open_slots:
            raise ValueError(
                f"touched leaves {touched.open_slots} members open, more than the "
                f"{declared.open_slots} the declared scope leaves open"
            )
    if declared is None or value is None:
        return
    if declared.value is None or not math.isclose(value, declared.value, rel_tol=1e-9, abs_tol=1e-9):
        raise ValueError(
            f"value={value} must restate the declared scope's {declared.covered}/{declared.total}"
        )


class MetricSurface(BaseModel):
    """One surface's independent measurement inside a multi-surface metric.

    Every field is required, which is the whole point: §5-B2's two classifiers are
    separate implementations over separate assertion populations, so a surface that
    appears here was measured *on that surface*, with its own denominator and its
    own evidence artifact. An entry cannot list a surface it merely assumes.
    """

    model_config = _FROZEN

    layer: MetricLayer
    value: float
    declared: MetricScope
    # The change-touched part of this surface's population; see `MetricEntry`.
    touched: MetricScope | None = None
    evidence: NonEmptyStr

    @model_validator(mode="after")
    def _the_scopes_and_the_value_agree(self) -> Self:
        _check_measurement(value=self.value, declared=self.declared, touched=self.touched)
        return self


class MetricEntry(BaseModel):
    """One metric's standing in this batch.

    Numeric and boolean metrics share the shape but not the field: ``value``
    holds the ratio or score that ``floor_ratio`` minimises over, and ``holds``
    states a deterministic check (``adversarial_clean``). Separate fields on
    purpose — §7 keeps boolean entries out of the ratio, and a bool riding inside
    a float field is exactly how one would get in. Which field a key may use, and
    whether it must publish a denominator at all, is not the entry's choice:
    ``METRIC_SHAPES`` decides and the document checks.

    ``value`` always restates the ``declared`` scope when there is one, so a floor
    declared against a plain key has exactly one meaning. A future
    ``target: touched`` floor reads ``touched.value`` instead, and a floor aimed at
    one surface of a multi-surface metric has to name that surface — the pooled
    ``value`` is what the canonical key means, and it is deliberately unaffected by
    how the population splits across surfaces.
    """

    model_config = _FROZEN

    layer: MetricLayer
    status: MetricStatus
    # Set only when `status == "evaluated"`, and then exactly one of value/holds.
    # `holds=False` is a measurement like any other: it is the boolean metric
    # reporting that its check did *not* hold.
    value: float | None = None
    holds: bool | None = None
    # The whole denominator this metric is a ratio over, absent for the scalar
    # metrics (`METRIC_SHAPES`).
    declared: MetricScope | None = None
    # The change-touched subset of `declared` — the scope a "touched must be 1.0"
    # floor judges (§8), stated here so no floor key has to encode it. `None`
    # means nobody computed the subset; `MetricScope.of(total=0, covered=0)` means
    # it was computed and is empty.
    touched: MetricScope | None = None
    # One entry per surface in `METRIC_SURFACES[key]`, or empty for the metrics
    # collected on a single surface. Sorted by layer at the boundary so a
    # producer's iteration order cannot change the replayed bytes.
    surfaces: tuple[MetricSurface, ...] = ()
    # The content-addressed artifact this measurement was folded from (§9).
    # Required of an evaluated entry — a measurement nothing can be traced back to
    # is not evidence — and deliberately not required of the other three states,
    # which have nothing to point at.
    evidence: str = ""

    @field_validator("surfaces", mode="after")
    @classmethod
    def _sort_the_surfaces(cls, value: tuple[MetricSurface, ...]) -> tuple[MetricSurface, ...]:
        return tuple(sorted(value, key=lambda surface: surface.layer))

    @property
    def scopes(self) -> tuple[MetricScope, ...]:
        """The tallies this entry carries, in declared-then-touched order."""
        return tuple(scope for scope in (self.declared, self.touched) if scope is not None)

    @model_validator(mode="after")
    def _the_scopes_and_the_value_agree(self) -> Self:
        _check_measurement(value=self.value, declared=self.declared, touched=self.touched)
        return self

    @model_validator(mode="after")
    def _only_an_evaluated_entry_measures_anything(self) -> Self:
        """Status and measurement are one fact, so they may not disagree.

        A ``not_evaluated`` entry carrying a value would be read as a measurement
        by anything that branches on the value first, and a ``collection_failed``
        one carrying a tally or a surface would let a failed collection still count
        toward a floor.
        """
        measured = [name for name in ("value", "holds") if getattr(self, name) is not None]
        if self.status == "evaluated":
            if len(measured) != 1:
                raise ValueError(
                    f"an evaluated metric states exactly one of value/holds, got {measured or 'neither'}"
                )
            return self
        if measured or self.scopes or self.surfaces:
            raise ValueError(
                f"a {self.status} metric measured nothing, so it carries no value, scope or surface"
            )
        return self

    @model_validator(mode="after")
    def _an_evaluated_entry_points_at_its_evidence(self) -> Self:
        if self.status == "evaluated" and not self.evidence:
            raise ValueError("an evaluated metric names the artifact it was folded from")
        return self

    @model_validator(mode="after")
    def _a_boolean_check_has_no_denominator(self) -> Self:
        if self.holds is not None and (self.scopes or self.surfaces):
            raise ValueError("a boolean metric is a deterministic check, not a ratio over a scope")
        return self

    @model_validator(mode="after")
    def _the_surfaces_pool_into_the_aggregate(self) -> Self:
        """The published aggregate must be the sum of the slices, not their mean.

        This is what makes "no averaging" mechanical rather than advisory. An
        average of two ratios would satisfy no arithmetic relation to the two
        tallies, so a producer that has measured one surface and guessed the other
        cannot publish an aggregate that adds up; one that pools two real
        populations always can. The same equality holds for ``touched`` when the
        aggregate states one, since a touched-target floor reads that number.
        """
        if not self.surfaces:
            return self
        layers = [surface.layer for surface in self.surfaces]
        if len(set(layers)) != len(layers):
            raise ValueError(f"surfaces list a layer twice: {sorted(layers)}")
        if self.declared is None:
            raise ValueError("a surface breakdown needs the aggregate tally it pools into")
        pooled = (
            sum(surface.declared.total for surface in self.surfaces),
            sum(surface.declared.covered for surface in self.surfaces),
        )
        if pooled != (self.declared.total, self.declared.covered):
            raise ValueError(
                f"the declared tally {self.declared.covered}/{self.declared.total} is not the pooled "
                f"{pooled[1]}/{pooled[0]} of its surfaces; an aggregate is their sum, never their mean"
            )
        if self.touched is None:
            return self
        touched = [surface.touched for surface in self.surfaces]
        if any(scope is None for scope in touched):
            raise ValueError(
                "the aggregate states a touched subset, so every surface states the part it contributed"
            )
        pooled_touched = (
            sum(scope.total for scope in touched if scope is not None),
            sum(scope.covered for scope in touched if scope is not None),
        )
        if pooled_touched != (self.touched.total, self.touched.covered):
            raise ValueError(
                f"the touched tally {self.touched.covered}/{self.touched.total} is not the pooled "
                f"{pooled_touched[1]}/{pooled_touched[0]} of its surfaces"
            )
        return self


class MetricCollectionGap(BaseModel):
    """One reason a metric could not be measured, in the vocabulary a gate reads.

    Not a free-text note: a non-empty ``collection_gaps`` is what routes a change
    to needs_human (§6), so every member states which metric it is about and which
    failure mode it is; only ``detail`` is prose.

    ``subject`` is what separates the two kinds of gap. Empty means the whole
    metric failed, and the entry it names must say ``collection_failed``.
    Non-empty names one member of the denominator — an entity, a constraint key,
    one artifact — and the rest of the metric stays measurable, so such a gap sits
    beside an evaluated partial measurement.
    """

    model_config = _FROZEN

    code: MetricCollectionGapCode
    metric: MetricKey
    # The entity, constraint key or artifact path the gap is about; "" when the
    # gap is about the metric as a whole.
    subject: str = ""
    detail: str = ""

    @property
    def is_whole_metric(self) -> bool:
        """Whether this gap sinks the metric rather than one member of it."""
        return not self.subject

    @model_validator(mode="after")
    def _the_code_and_the_subject_agree(self) -> Self:
        """A code that can only mean one scale may not claim the other.

        Without this, ``collection_failed`` with a subject would look like a
        survivable partial failure and let the metric stay evaluated, which is the
        fail-open §6 was written against; and ``entity_without_constraints``
        without a subject would sink a whole metric over one undeclared entity.
        """
        if self.code in WHOLE_METRIC_GAP_CODES and self.subject:
            raise ValueError(f"{self.code} is a whole-metric failure and carries no subject")
        if self.code in SUBJECT_SCOPED_GAP_CODES and not self.subject:
            raise ValueError(f"{self.code} is about one member of the denominator and needs a subject")
        return self


class MetricShortboard(BaseModel):
    """One metric falling short in a way that reports rather than blocks (§6)."""

    model_config = _FROZEN

    code: MetricShortboardCode
    metric: MetricKey
    detail: str = ""


class RiskDeclarationLowered(BaseModel):
    """One case that declared a risk level below its own mechanical bound.

    Published per case rather than as an aggregate flag because the aggregate
    hides exactly the signal this exists for: ``max`` over declarations is above
    the bound as soon as *one* case declares ``critical``, so a P0 case declaring
    ``low`` beside it leaves no trace. A retro asking "which authoring runs
    undersell risk" needs the case, not a boolean.
    """

    model_config = _FROZEN

    case_id: CaseId
    declared: RiskTier
    # The bound this case alone implies, from its own priority and severity.
    lower_bound: RiskTier

    @model_validator(mode="after")
    def _the_declaration_is_actually_lower(self) -> Self:
        if RISK_TIER_ORDER.index(self.declared) >= RISK_TIER_ORDER.index(self.lower_bound):
            raise ValueError(
                f"{self.case_id} declared {self.declared!r} at or above its own bound "
                f"{self.lower_bound!r}, so it lowered nothing"
            )
        return self


@runtime_checkable
class RiskTierFacts(Protocol):
    """What `MetricsDocument.of` needs to publish a checkable risk tier.

    A structural type rather than an import: the resolution is computed in the
    evidence layer (``evidence.risk_tier.RiskTierResolution``) and the artifact
    layer must stay loadable by a consumer that has no evaluator — the same reason
    `trace_sufficiency.py` spells out vocabularies instead of importing them.
    """

    @property
    def tier(self) -> RiskTier: ...

    @property
    def lower_bound(self) -> RiskTier: ...

    @property
    def declared(self) -> RiskTier | None: ...

    @property
    def declaration_lowered(self) -> bool: ...

    @property
    def lowered_declarations(self) -> tuple[RiskDeclarationLowered, ...]: ...


# A read-only mapping at runtime, an ordinary JSON object on the wire. The
# annotation is `Mapping` (not `dict`) so the frozen document can hand out a
# `MappingProxyType`, and the serializer converts that back to a `dict` under the
# `dict[MetricKey, MetricEntry]` schema so nested entries still serialise normally
# and the artifact's shape is unchanged.
MetricMap = Annotated[
    Mapping[MetricKey, MetricEntry],
    PlainSerializer(dict, return_type=dict[MetricKey, MetricEntry]),
]


class MetricsDocument(BaseModel):
    model_config = _FROZEN

    schema_version: Literal["2"]
    change_id: NonEmptyStr
    cadence: MetricCadence
    # Injected by the caller and deliberately outside the replay anchor: the
    # aggregation is a pure function of its input artifacts, and a clock read
    # inside it would make "same inputs, same bytes" untestable (§4, §9).
    computed_at: AwareDatetime
    # All five risk facts, because four of them are what make the first checkable
    # (§7), and none of them has a default: a document that omitted the
    # declaration or the audit would read as "nobody declared anything", which is
    # a claim, not an absence. `risk_tier` selects the floor band;
    # `risk_tier_lower_bound` is what priority/severity mechanically imply;
    # `risk_tier_declared` is the highest level the case authors asked for, or None
    # if none did; `risk_declaration_lowered` and `risk_lowered_declarations` are
    # the underselling signal, the second per case so one case's `critical` cannot
    # mask another's `low`.
    risk_tier: RiskTier
    risk_tier_lower_bound: RiskTier
    risk_tier_declared: RiskTier | None
    risk_declaration_lowered: bool
    risk_lowered_declarations: tuple[RiskDeclarationLowered, ...]
    # Keyed by the closed vocabulary, so a metric nobody declared cannot appear
    # and a floor cannot be declared against a metric that never publishes.
    metrics: MetricMap
    collection_gaps: tuple[MetricCollectionGap, ...] = ()
    shortboards: tuple[MetricShortboard, ...] = ()
    # None when nothing numeric was evaluated. Never a probability of
    # correctness; see the module docstring.
    floor_ratio: FloorRatio | None = None
    policy_digest: str = Field(pattern=_POLICY_DIGEST)

    @field_validator("metrics", mode="after")
    @classmethod
    def _freeze_the_metric_map(cls, value: MetricMap) -> MetricMap:
        """Copy the caller's mapping and hand back a read-only view of the copy.

        ``frozen=True`` stops the *field* from being rebound; it does nothing
        about ``document.metrics[key] = other``, and a validated document that a
        consumer can still edit is a validated document in name only. The copy
        matters as much as the proxy: without it the caller's dict stays a live
        handle on the document's contents.
        """
        return MappingProxyType(dict(value))

    @field_validator("risk_lowered_declarations", mode="after")
    @classmethod
    def _order_the_lowering_audit(
        cls, value: tuple[RiskDeclarationLowered, ...]
    ) -> tuple[RiskDeclarationLowered, ...]:
        """One case appears once, and the order is the case order.

        Sorted at the boundary for the same reason `uncovered` is: the fold order
        of the cases must not change the published bytes. Duplicates are refused
        because two rows for one case would double-count the signal a retro
        aggregates.
        """
        case_ids = [lowered.case_id for lowered in value]
        if len(set(case_ids)) != len(case_ids):
            raise ValueError("risk_lowered_declarations names a case twice")
        return tuple(sorted(value, key=lambda lowered: lowered.case_id))

    @classmethod
    def of(
        cls,
        *,
        risk: RiskTierFacts,
        change_id: str,
        cadence: MetricCadence,
        computed_at: datetime,
        metrics: Mapping[MetricKey, MetricEntry],
        policy_digest: str,
        collection_gaps: tuple[MetricCollectionGap, ...] = (),
        shortboards: tuple[MetricShortboard, ...] = (),
        floor_ratio: float | None = None,
    ) -> MetricsDocument:
        """Publish a document whose five risk fields come from one resolution.

        The producer's only handoff for the risk band: the mechanical lower bound
        is a fold over the change's cases, which this document does not have and
        cannot recompute, so `evidence.risk_tier.resolve_risk_tier` computes it
        once and every published field is read off that result here. The
        validators below would catch a producer that restated a field wrongly, but
        catching it is second best. ``schema_version`` is supplied here for the
        same reason — it is not a producer's choice.
        """
        return cls(
            schema_version="2",
            change_id=change_id,
            cadence=cadence,
            computed_at=computed_at,
            risk_tier=risk.tier,
            risk_tier_lower_bound=risk.lower_bound,
            risk_tier_declared=risk.declared,
            risk_declaration_lowered=risk.declaration_lowered,
            risk_lowered_declarations=risk.lowered_declarations,
            metrics=metrics,
            collection_gaps=collection_gaps,
            shortboards=shortboards,
            floor_ratio=floor_ratio,
            policy_digest=policy_digest,
        )

    @model_validator(mode="after")
    def _the_risk_facts_add_up(self) -> Self:
        """§7: the tier is `max(mechanical bound, declaration)`, and it is checked.

        ``risk.level`` had no schema at all, and the one observed conflict (a P0
        case labelled ``medium``) was resolved *downward* by a reviewing model. So
        the document publishes the inputs and the model recomputes the outputs
        from them: a tier below its own bound, or a tier that ignores a higher
        declaration, is refused rather than routed on.

        The lowering flag is checked against the per-case audit rather than
        against the aggregate declaration, because the aggregate cannot express
        it: with one case declaring ``critical`` and another ``low``, the maximum
        is above the bound and the ``low`` is still a case that undersold its own
        risk. Each audited case must also be *possible* — no case's own bound can
        exceed the aggregate, which is a max over exactly those bounds.
        """
        rank = RISK_TIER_ORDER.index
        if rank(self.risk_tier) < rank(self.risk_tier_lower_bound):
            raise ValueError(
                f"risk_tier={self.risk_tier!r} is below the mechanical lower bound "
                f"{self.risk_tier_lower_bound!r}; a declared level may only raise it"
            )
        declared = self.risk_tier_declared
        expected_tier = (
            self.risk_tier_lower_bound
            if declared is None or rank(declared) <= rank(self.risk_tier_lower_bound)
            else declared
        )
        if self.risk_tier != expected_tier:
            raise ValueError(
                f"risk_tier={self.risk_tier!r} is not max(lower_bound="
                f"{self.risk_tier_lower_bound!r}, declared={declared!r}) = {expected_tier!r}"
            )
        if self.risk_declaration_lowered != bool(self.risk_lowered_declarations):
            raise ValueError(
                f"risk_declaration_lowered={self.risk_declaration_lowered} does not match the "
                f"{len(self.risk_lowered_declarations)} lowered declarations audited"
            )
        for lowered in self.risk_lowered_declarations:
            if rank(lowered.lower_bound) > rank(self.risk_tier_lower_bound):
                raise ValueError(
                    f"{lowered.case_id} claims a bound {lowered.lower_bound!r} above the change's "
                    f"{self.risk_tier_lower_bound!r}, which is the max over exactly those bounds"
                )
        return self

    @model_validator(mode="after")
    def _each_entry_answers_in_the_shape_its_metric_declares(self) -> Self:
        """An entry cannot be the wrong kind of answer to its own key.

        The key is what a floor names, so the key decides whether the answer is a
        ratio, a scalar or a check. Each direction fails open in its own way: a
        `holds=True` on a numeric key would be a metric that never enters
        `floor_ratio` and so never falls short of anything; a bare `1.0` on a
        `scoped_ratio` key is a coverage claim with no denominator to audit it
        against, which is how "0/0 = 1.0" survives review; and a denominator on a
        scalar key invites a floor over a set nobody defined.
        """
        for key, entry in self.metrics.items():
            shape = METRIC_SHAPES[key]
            if shape == "boolean":
                if entry.value is not None:
                    raise ValueError(f"{key} is a boolean metric and answers with holds, not value")
            elif entry.holds is not None:
                raise ValueError(f"{key} is a numeric metric and answers with value, not holds")
            if entry.status != "evaluated":
                continue
            if shape == "scoped_ratio" and entry.declared is None:
                raise ValueError(
                    f"{key} is a ratio over a declared denominator, so an evaluated entry publishes "
                    f"the tally it came from rather than a bare value"
                )
            if shape == "scalar" and entry.scopes:
                raise ValueError(
                    f"{key} is a scalar with no denominator of obligations; its detail belongs in "
                    f"its evidence artifact, not in a scope"
                )
        return self

    @model_validator(mode="after")
    def _each_entry_claims_a_layer_and_a_breakdown_its_metric_declares(self) -> Self:
        """The surfaces a metric spans are the metric's property, not the entry's.

        `METRIC_SURFACES` is what stops a two-classifier metric (§5-B2) from
        publishing one number for both surfaces, and stops every other metric from
        inventing a breakdown that no collector produces.
        """
        for key, entry in self.metrics.items():
            allowed = METRIC_LAYERS[key]
            if entry.layer not in allowed:
                raise ValueError(
                    f"{key} is not collected on the {entry.layer!r} layer; expected one of {sorted(allowed)}"
                )
            required = METRIC_SURFACES[key]
            present = {surface.layer for surface in entry.surfaces}
            if not required:
                if entry.surfaces:
                    raise ValueError(f"{key} is collected on one surface and publishes no breakdown")
                continue
            if entry.status == "evaluated" and present != required:
                raise ValueError(
                    f"{key} is measured by one classifier per surface, so an evaluated entry publishes "
                    f"each of {sorted(required)} independently; got {sorted(present)}"
                )
        return self

    @model_validator(mode="after")
    def _every_gap_and_shortboard_names_a_published_metric(self) -> Self:
        """A gap or shortboard about an absent metric is dangling: the operator it
        is addressed to has no entry to look at, and a gate counting gaps would
        route on a metric this document never measured."""
        for label, keys in (
            ("collection_gaps", {gap.metric for gap in self.collection_gaps}),
            ("shortboards", {board.metric for board in self.shortboards}),
        ):
            dangling = sorted(keys - set(self.metrics))
            if dangling:
                raise ValueError(f"{label} name metrics this document does not publish: {dangling}")
        return self

    @model_validator(mode="after")
    def _a_whole_metric_failure_and_its_gap_agree(self) -> Self:
        """The failure and the gap list are one fact, stated twice (§6).

        Both directions are refused because both fail open in their own way: a
        ``collection_failed`` entry with no whole-metric gap reaches a gate that
        routes on ``collection_gaps`` and is read as a pass, and a whole-metric gap
        beside a measured entry lets that measurement satisfy a floor the failed
        collection never earned. Subject-scoped gaps are exempt by design — they
        are the *partial* case, and an evaluated metric beside them is the point.
        """
        whole_metric_gaps = {gap.metric for gap in self.collection_gaps if gap.is_whole_metric}
        for key, entry in self.metrics.items():
            if entry.status == "collection_failed" and key not in whole_metric_gaps:
                raise ValueError(
                    f"{key} failed collection but no whole-metric gap in collection_gaps says why"
                )
            if key in whole_metric_gaps and entry.status != "collection_failed":
                raise ValueError(
                    f"{key} has a whole-metric collection gap but reports status={entry.status!r}"
                )
        return self

    @model_validator(mode="after")
    def _floor_ratio_is_the_minimum_of_something(self) -> Self:
        """§7: the ratio is a minimum over *evaluated numeric* metrics.

        With none of those there is nothing to take a minimum of, and a stored
        number would be a claim about metrics that were never measured — the
        vacuous success the whole design exists to refuse.
        """
        if self.floor_ratio is not None and not any(
            entry.status == "evaluated" and METRIC_KINDS[key] == "numeric"
            for key, entry in self.metrics.items()
        ):
            raise ValueError("floor_ratio is a minimum over evaluated numeric metrics; none were evaluated")
        return self

    def replay_subtree(self) -> dict[str, Any]:
        """The normalized subtree a replay compares, per §9's anchor.

        ``computed_at`` is excluded (it is injected, not computed) and every
        collection is put in a total order — metrics by key, gaps and shortboards
        by their whole content, and the per-metric collections (`uncovered`,
        `surfaces`) already normalized at their own boundary — so "same inputs
        replay to the same bytes" is a property of the aggregation rather than of
        the order a producer happened to append in. Defined here, on the document,
        so the aggregation operation and the test that pins its determinism cannot
        disagree about what the anchor covers.
        """
        return {
            "metrics": {key: self.metrics[key].model_dump(mode="json") for key in sorted(self.metrics)},
            "collection_gaps": sorted(
                (gap.model_dump(mode="json") for gap in self.collection_gaps),
                key=lambda gap: (gap["metric"], gap["code"], gap["subject"], gap["detail"]),
            ),
            "shortboards": sorted(
                (board.model_dump(mode="json") for board in self.shortboards),
                key=lambda board: (board["metric"], board["code"], board["detail"]),
            ),
            "floor_ratio": self.floor_ratio,
        }


__all__ = [
    "ARTIFACT_REL_BY_CADENCE",
    "METRIC_KEYS",
    "METRIC_KINDS",
    "METRIC_LAYERS",
    "METRIC_SHAPES",
    "METRIC_SURFACES",
    "NIGHTLY_METRICS_REL",
    "PR_METRICS_REL",
    "SUBJECT_SCOPED_GAP_CODES",
    "WHOLE_METRIC_GAP_CODES",
    "FloorRatio",
    "MetricCadence",
    "MetricCollectionGap",
    "MetricCollectionGapCode",
    "MetricEntry",
    "MetricKey",
    "MetricKind",
    "MetricLayer",
    "MetricMap",
    "MetricScope",
    "MetricShape",
    "MetricShortboard",
    "MetricShortboardCode",
    "MetricStatus",
    "MetricSurface",
    "MetricsDocument",
    "RiskDeclarationLowered",
    "RiskTierFacts",
]
