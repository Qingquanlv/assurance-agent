"""The risk band a change's floors are chosen by (metrics design §7).

The band cannot be the authored ``risk.level``. That field reached the models
with no schema at all, and the one observed conflict went the wrong way: a
case-design model labelled a P0 case ``medium``, a reviewing model noticed the
conflict with its own guidance, and resolved it by *accepting the lower label*
(run RET-api-management-20260804-115008). Selecting an evidence floor from a
number the authoring model is free to write down is a floor the authoring model
controls.

So the band is ``max(mechanical bound, declared levels)``, where the bound comes
from ``priority``/``severity`` — fields the same author writes, but which are read
by many other mechanisms and therefore not free to be quietly softened. The
declaration keeps its one useful power, raising the band above what the mechanics
noticed, and loses the other.

The resolution is returned as an object rather than a bare tier because the two
inputs are what make a stored tier interpretable: ``high`` alone cannot say
whether an author asked for it, and the lowering audit is the signal a retro needs
to see an authoring model that keeps underselling risk. That audit is kept **per
case** because the aggregate cannot express it — one case declaring ``critical``
puts the maximum declaration above the bound, and a P0 case declaring ``low``
beside it would leave no trace at all.

The resolution is what `MetricsDocument.of` publishes: the bound is a fold over
cases, which the artifact cannot recompute, so it is resolved once here and handed
over whole.

Which cases to pass in is the caller's decision (the metrics aggregation passes
the change's required cases). No cases bounds at ``low``: there is nothing whose
risk could raise it.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from assurance_agent.artifacts.models.cases import CaseEntry, CasePriority, CaseSeverity
from assurance_agent.artifacts.models.common import RISK_TIER_ORDER, RiskTier
from assurance_agent.artifacts.models.metrics import RiskDeclarationLowered

# Both tables are total over their vocabulary, and both are consulted for every
# case: a P3 blocker and a P0 trivial-severity case are each raised by the axis
# that noticed. `test_every_priority_and_severity_value_has_a_bound` is what makes
# a new enum value red here instead of defaulting into the lowest band.
_PRIORITY_LOWER_BOUNDS: dict[CasePriority, RiskTier] = {
    "P0": "high",
    "P1": "medium",
    "P2": "low",
    "P3": "low",
}
_SEVERITY_LOWER_BOUNDS: dict[CaseSeverity, RiskTier] = {
    "blocker": "high",
    "critical": "high",
    "major": "medium",
    "minor": "low",
}


def _higher(left: RiskTier, right: RiskTier) -> RiskTier:
    return left if RISK_TIER_ORDER.index(left) >= RISK_TIER_ORDER.index(right) else right


def _case_lower_bound(case: CaseEntry) -> RiskTier:
    """The bound both axes of one case imply.

    A missing table entry raises rather than defaulting: a priority or severity
    value with no bound would resolve to ``low``, i.e. the loosest floors, which
    is the direction a mistake must never take.
    """
    try:
        by_priority = _PRIORITY_LOWER_BOUNDS[case.priority]
        by_severity = _SEVERITY_LOWER_BOUNDS[case.severity]
    except KeyError as exc:
        raise ValueError(f"{case.case_id}: no risk tier lower bound declared for {exc.args[0]!r}") from None
    return _higher(by_priority, by_severity)


def mechanical_lower_bound(cases: Iterable[CaseEntry]) -> RiskTier:
    """The highest band any of ``cases`` mechanically implies; ``low`` for none."""
    bound: RiskTier = "low"
    for case in cases:
        bound = _higher(bound, _case_lower_bound(case))
    return bound


@dataclass(frozen=True)
class RiskTierResolution:
    """The band, the two inputs that decided it, and who tried to lower it.

    ``tier`` is what selects floors. The rest is kept so a stored tier stays
    interpretable and so the *disagreement* stays visible: an authoring model whose
    declarations keep landing below the mechanical bound is a retro signal, and it
    is invisible once the numbers are collapsed into one.
    """

    tier: RiskTier
    lower_bound: RiskTier
    # The highest level any case declared, or None when no case stated one.
    declared: RiskTier | None
    # Every case that declared below its own bound, sorted by case id. Per case
    # rather than a count, because the actionable question a retro asks is *which*
    # case, and because a count cannot be checked against anything.
    lowered_declarations: tuple[RiskDeclarationLowered, ...] = ()

    @property
    def declaration_raised(self) -> bool:
        """Whether the declaration pushed the band above the mechanical bound."""
        if self.declared is None:
            return False
        return RISK_TIER_ORDER.index(self.declared) > RISK_TIER_ORDER.index(self.lower_bound)

    @property
    def declaration_lowered(self) -> bool:
        """Whether *any* case asked for less than the mechanics found for it.

        Named for the attempt, not the outcome: ``tier`` already ignored it. Read
        off the per-case audit rather than off ``declared``, because the aggregate
        answers a different question — one case declaring ``critical`` lifts the
        maximum above the bound while another case's ``low`` goes unnoticed.
        """
        return bool(self.lowered_declarations)


def resolve_risk_tier(cases: Iterable[CaseEntry]) -> RiskTierResolution:
    """Resolve the floor-selecting band for ``cases`` (§7's max, in both senses)."""
    materialized = tuple(cases)
    bound = mechanical_lower_bound(materialized)
    declared: RiskTier | None = None
    lowered: list[RiskDeclarationLowered] = []
    for case in materialized:
        if case.risk is None:
            continue
        level = case.risk.level
        declared = level if declared is None else _higher(declared, level)
        case_bound = _case_lower_bound(case)
        if RISK_TIER_ORDER.index(level) < RISK_TIER_ORDER.index(case_bound):
            lowered.append(
                RiskDeclarationLowered(case_id=case.case_id, declared=level, lower_bound=case_bound)
            )
    return RiskTierResolution(
        tier=bound if declared is None else _higher(bound, declared),
        lower_bound=bound,
        declared=declared,
        lowered_declarations=tuple(sorted(lowered, key=lambda entry: entry.case_id)),
    )


__all__ = ["RiskTierResolution", "mechanical_lower_bound", "resolve_risk_tier"]
