"""PR-cadence metrics, mutation, assertion, drift, and adversarial handlers."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


from assurance_intake.contracts import RiskTier
from assurance_intake.contracts.cases import CaseEntryAuthoring
from assurance_quality.contracts.metrics import (
    MetricEntry,
    MetricKey,
    MetricShortboard,
    MetricsDocument,
    RiskDeclarationLowered,
)
from assurance_quality.contracts.pr_metrics import (
    AdversarialYieldEvidence,
    AssertionStrengthEvidence,
    AuthMatrixEvidence,
    BaselineDriftEvidence,
    ConstraintCoverageEvidence,
    CoverageDiffEvidence,
    JourneyCoverageEvidence,
    MutationEvidence,
    PerfSlackEvidence,
)
from assurance_quality.operations.common import json_digest

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class BoundRisk:
    def __init__(
        self,
        *,
        tier: RiskTier = "medium",
        lower_bound: RiskTier = "medium",
        declared: RiskTier | None = None,
        lowered: tuple[RiskDeclarationLowered, ...] = (),
    ) -> None:
        resolved: RiskTier
        if declared is None or _rank(declared) <= _rank(lower_bound):
            resolved = lower_bound
        else:
            resolved = declared
        del tier
        self.tier: RiskTier = resolved
        self.lower_bound: RiskTier = lower_bound
        self.declared: RiskTier | None = declared
        self.declaration_lowered = bool(lowered)
        self.lowered_declarations = lowered


def _rank(tier: RiskTier) -> int:
    return ("low", "medium", "high", "critical").index(tier)


_PRIORITY_BOUND: Mapping[str, RiskTier] = {
    "P0": "critical",
    "P1": "high",
    "P2": "medium",
    "P3": "low",
}
_SEVERITY_BOUND: Mapping[str, RiskTier] = {
    "blocker": "critical",
    "critical": "high",
    "major": "medium",
    "minor": "low",
}


def resolve_case_risk(cases: tuple[CaseEntryAuthoring, ...]) -> BoundRisk:
    if not cases:
        raise ValueError("risk resolution requires at least one reviewed Case")
    lower_bound: RiskTier = "low"
    declared: RiskTier = "low"
    lowered: list[RiskDeclarationLowered] = []
    for case in cases:
        case_bound = max(
            (_PRIORITY_BOUND[case.priority], _SEVERITY_BOUND[case.severity]),
            key=_rank,
        )
        lower_bound = max((lower_bound, case_bound), key=_rank)
        declared = max((declared, case.risk.level), key=_rank)
        if _rank(case.risk.level) < _rank(case_bound):
            lowered.append(
                RiskDeclarationLowered(
                    case_id=case.case_id,
                    declared=case.risk.level,
                    lower_bound=case_bound,
                )
            )
    return BoundRisk(
        lower_bound=lower_bound,
        declared=declared,
        lowered=tuple(sorted(lowered, key=lambda item: item.case_id)),
    )


class RiskInput(BaseModel):
    model_config = _FROZEN

    tier: RiskTier = "medium"
    lower_bound: RiskTier = "medium"
    declared: RiskTier | None = None


class PrEvidenceBundle(BaseModel):
    model_config = _FROZEN

    change_id: str
    batch_id: str
    computed_at: datetime = Field(default_factory=lambda: datetime(2026, 8, 22, tzinfo=UTC))
    policy_digest: str
    risk: RiskInput = RiskInput()
    cadence: Literal["pr", "nightly"] = "pr"
    diff: CoverageDiffEvidence | None = None
    constraint: ConstraintCoverageEvidence | None = None
    auth: AuthMatrixEvidence | None = None
    journey: JourneyCoverageEvidence | None = None
    slack: PerfSlackEvidence | None = None
    mutation: MutationEvidence | None = None
    assertion: AssertionStrengthEvidence | None = None
    drift: BaselineDriftEvidence | None = None
    adversarial: AdversarialYieldEvidence | None = None


def _entry(
    *,
    layer: Literal["api", "e2e", "fuzz", "performance", "backend", "cross"],
    status: Literal["evaluated", "not_evaluated", "skipped", "collection_failed"] = "skipped",
    value: float | None = None,
    holds: bool | None = None,
    declared: object = None,
    evidence: str = "",
    surfaces: tuple[object, ...] = (),
) -> MetricEntry:
    return MetricEntry(
        layer=layer,
        status=status,
        value=value,
        holds=holds,
        declared=declared,  # type: ignore[arg-type]
        evidence=evidence,
        surfaces=surfaces,  # type: ignore[arg-type]
    )


def _skipped(layer: Literal["api", "e2e", "fuzz", "performance", "backend", "cross"]) -> MetricEntry:
    return _entry(layer=layer, status="skipped")


def build_metrics_document(bundle: PrEvidenceBundle) -> MetricsDocument:
    metrics: dict[MetricKey, MetricEntry] = {
        "diff_coverage": _skipped("backend"),
        "constraint_coverage": _skipped("api"),
        "auth_matrix_coverage": _skipped("api"),
        "journey_coverage": _skipped("e2e"),
        "baseline_drift": _skipped("performance"),
        "mutation_score": _skipped("backend"),
        "assertion_strength": _skipped("cross"),
        "threshold_slack": _skipped("performance"),
        "adversarial_yield": _skipped("cross") if False else _skipped("api"),
        "adversarial_clean": _skipped("cross"),
    }
    shortboards: list[MetricShortboard] = []
    if bundle.diff is not None:
        metrics["diff_coverage"] = _entry(
            layer="backend",
            status="evaluated" if bundle.diff.value is not None else "not_evaluated",
            value=bundle.diff.value,
            evidence=json_digest(bundle.diff.model_dump(mode="json")),
        )
        shortboards.extend(bundle.diff.shortboards)
    if bundle.constraint is not None and bundle.constraint.declared is not None:
        metrics["constraint_coverage"] = _entry(
            layer="api",
            status="evaluated",
            value=bundle.constraint.value,
            declared=bundle.constraint.declared,
            evidence=json_digest(bundle.constraint.model_dump(mode="json")),
        )
    if bundle.auth is not None and bundle.auth.declared is not None:
        metrics["auth_matrix_coverage"] = _entry(
            layer="api",
            status="evaluated",
            value=bundle.auth.value,
            declared=bundle.auth.declared,
            evidence=json_digest(bundle.auth.model_dump(mode="json")),
        )
    if bundle.journey is not None and bundle.journey.declared is not None:
        metrics["journey_coverage"] = _entry(
            layer="e2e",
            status="evaluated",
            value=bundle.journey.value,
            declared=bundle.journey.declared,
            evidence=json_digest(bundle.journey.model_dump(mode="json")),
        )
    if bundle.slack is not None:
        metrics["threshold_slack"] = _entry(
            layer="performance",
            status="evaluated" if bundle.slack.value is not None else "not_evaluated",
            value=bundle.slack.value,
            evidence=json_digest(bundle.slack.model_dump(mode="json")),
        )
        shortboards.extend(bundle.slack.shortboards)
    if bundle.mutation is not None:
        metrics["mutation_score"] = _entry(
            layer="backend",
            status=bundle.mutation.status
            if bundle.mutation.status != "collection_failed"
            else "collection_failed",
            value=bundle.mutation.value,
            evidence=json_digest(bundle.mutation.model_dump(mode="json")),
        )
        shortboards.extend(bundle.mutation.shortboards)
    if (
        bundle.assertion is not None
        and bundle.assertion.status == "evaluated"
        and bundle.assertion.declared is not None
    ):
        metrics["assertion_strength"] = MetricEntry(
            layer="cross",
            status="evaluated",
            value=bundle.assertion.value,
            declared=bundle.assertion.declared,
            evidence=json_digest(bundle.assertion.model_dump(mode="json")),
            surfaces=tuple(
                {
                    "layer": surface.layer,
                    "value": surface.value if surface.value is not None else 0.0,
                    "declared": surface.declared.model_dump(mode="json"),
                    "evidence": json_digest(surface.model_dump(mode="json")),
                }
                for surface in bundle.assertion.surfaces
            ),  # type: ignore[arg-type]
        )
    if bundle.drift is not None:
        metrics["baseline_drift"] = _entry(
            layer="performance",
            status=bundle.drift.status if bundle.drift.status != "collection_failed" else "collection_failed",
            value=bundle.drift.value,
            evidence=json_digest(bundle.drift.model_dump(mode="json")),
        )
        shortboards.extend(bundle.drift.shortboards)
    if bundle.adversarial is not None:
        metrics["adversarial_yield"] = _entry(
            layer=bundle.adversarial.layer,
            status=bundle.adversarial.status
            if bundle.adversarial.status != "collection_failed"
            else "collection_failed",
            value=bundle.adversarial.value,
            evidence=json_digest(bundle.adversarial.model_dump(mode="json")),
        )
        metrics["adversarial_clean"] = _entry(
            layer="cross",
            status="evaluated",
            holds=bundle.adversarial.unclosed_count == 0,
            evidence=json_digest(bundle.adversarial.model_dump(mode="json")),
        )
        shortboards.extend(bundle.adversarial.shortboards)
    numeric = [
        entry.value
        for key, entry in metrics.items()
        if entry.status == "evaluated" and entry.value is not None
    ]
    risk = BoundRisk(
        tier=bundle.risk.tier, lower_bound=bundle.risk.lower_bound, declared=bundle.risk.declared
    )
    return MetricsDocument.of(
        risk=risk,
        change_id=bundle.change_id,
        cadence=bundle.cadence,
        computed_at=bundle.computed_at,
        metrics=metrics,
        policy_digest=bundle.policy_digest,
        shortboards=tuple(shortboards),
        floor_ratio=min(1.0, min(numeric)) if numeric else None,
    )
