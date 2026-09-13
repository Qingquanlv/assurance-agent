from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

import pytest
from pydantic import ValidationError

from assurance_intake.contracts.cases import CaseEntryAuthoring
from assurance_intake.contracts.common import RiskTier
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1
from assurance_quality.contracts.coverage import classify_coverage_state
from assurance_quality.contracts.goal_policy import (
    ActiveCoverageScopeV1,
    CoverageFloorsV1,
    CoverageGoalPolicyV1,
    SufficiencyPolicyV1,
)
from assurance_quality.contracts.metrics import (
    METRIC_KEYS,
    MetricCollectionGap,
    MetricEntry,
    MetricKey,
    MetricScope,
    MetricsDocument,
)
from assurance_quality.contracts.sufficiency import (
    SufficiencyReasonCode,
    TraceInsufficientCase,
    TraceSufficiencyFacts,
)
from assurance_quality.operations.metrics import BoundRisk, resolve_case_risk

_SHA = "a" * 64
_CHANGE = "CH-1"
_CASE = "TC_AUTH_001"
_LAYERS = {
    "diff_coverage": "backend",
    "constraint_coverage": "api",
    "auth_matrix_coverage": "api",
    "journey_coverage": "e2e",
    "baseline_drift": "performance",
    "mutation_score": "backend",
    "assertion_strength": "cross",
    "threshold_slack": "performance",
    "adversarial_yield": "api",
    "adversarial_clean": "cross",
}


def test_quality_reexports_intake_owned_policy_types() -> None:
    from assurance_quality.contracts.goal_policy import (
        CoverageFloorsV1 as QualityFloors,
        CoverageGoalPolicyV1 as QualityGoalPolicy,
        SufficiencyPolicyV1 as QualitySufficiencyPolicy,
    )
    from assurance_intake.contracts.quality_goals import (
        CoverageFloorsV1 as IntakeFloors,
        CoverageGoalPolicyV1 as IntakeGoalPolicy,
        SufficiencyPolicyV1 as IntakeSufficiencyPolicy,
    )

    assert QualityFloors is IntakeFloors
    assert QualityGoalPolicy is IntakeGoalPolicy
    assert QualitySufficiencyPolicy is IntakeSufficiencyPolicy


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -0.01, 1.01])
def test_floor_must_be_finite_unit_interval(value: float) -> None:
    with pytest.raises(ValidationError):
        CoverageFloorsV1(low=value, medium=0.8, high=0.9, critical=1.0)


def test_goal_and_sufficiency_policy_are_closed_and_explicit() -> None:
    with pytest.raises(ValidationError):
        CoverageGoalPolicyV1.model_validate(
            {"coverage_floor_by_tier": {"low": 0.7, "medium": 0.8, "high": 0.9}}
        )
    with pytest.raises(ValidationError):
        SufficiencyPolicyV1(recency_hours=0, require_current_batch=True)
    with pytest.raises(ValidationError):
        SufficiencyPolicyV1.model_validate({"recency_hours": 24, "require_current_batch": False})


def test_goal_policies_extract_from_full_product_policy_without_hidden_defaults() -> None:
    document = {
        "coverage_floor_by_tier": {
            "low": 0.7,
            "medium": 0.8,
            "high": 0.9,
            "critical": 1.0,
        },
        "evidence_sufficiency": {"recency_hours": 24, "require_current_batch": True},
        "organization_rule": {"owner": "quality"},
    }
    assert CoverageGoalPolicyV1.from_product_policy(document).coverage_floor_by_tier.high == 0.9
    assert SufficiencyPolicyV1.from_product_policy(document).recency_hours == 24
    with pytest.raises(ValueError, match="policy_error"):
        CoverageGoalPolicyV1.from_product_policy({})
    with pytest.raises(ValueError, match="policy_error"):
        SufficiencyPolicyV1.from_product_policy({})


def _policy() -> CoverageGoalPolicyV1:
    return CoverageGoalPolicyV1(
        coverage_floor_by_tier=CoverageFloorsV1(
            low=0.70,
            medium=0.80,
            high=0.90,
            critical=1.0,
        )
    )


def _scope(
    *,
    risk_tier: RiskTier = "high",
    goals: tuple[str, ...] = ("constraint_coverage",),
    selected: tuple[str, ...] = ("api",),
) -> ActiveCoverageScopeV1:
    return ActiveCoverageScopeV1.model_validate(
        {
            "change_id": _CHANGE,
            "coverage_epoch": 0,
            "required_case_ids": (_CASE,),
            "selected_families": selected,
            "applicable_goals": goals,
            "applicability_refs": (
                EvidenceArtifactRefV1(
                    path="qa/results/preparation/applicability.json",
                    digest=_SHA,
                ),
            ),
            "risk_tier": risk_tier,
            "policy_digest": _SHA,
        }
    )


def _metrics(
    *,
    value: float = 0.90,
    status: str = "evaluated",
    risk_tier: RiskTier = "high",
    policy_digest: str = _SHA,
) -> MetricsDocument:
    metrics: dict[MetricKey, MetricEntry] = {
        key: MetricEntry(layer=cast(object, _LAYERS[key]), status="skipped")  # type: ignore[arg-type]
        for key in METRIC_KEYS
    }
    gaps: tuple[MetricCollectionGap, ...] = ()
    if status == "evaluated":
        covered = round(value * 10)
        scope = MetricScope.of(total=10, covered=covered)
        metrics["constraint_coverage"] = MetricEntry(
            layer="api",
            status="evaluated",
            value=scope.value,
            declared=scope,
            evidence="constraint-evidence",
        )
    elif status == "collection_failed":
        metrics["constraint_coverage"] = MetricEntry(layer="api", status="collection_failed")
        gaps = (
            MetricCollectionGap(
                code="collection_failed",
                metric="constraint_coverage",
            ),
        )
    else:
        metrics["constraint_coverage"] = MetricEntry(
            layer="api",
            status=cast(object, status),  # type: ignore[arg-type]
        )
    return MetricsDocument.of(
        risk=BoundRisk(lower_bound=risk_tier),
        change_id=_CHANGE,
        cadence="pr",
        computed_at=datetime(2026, 9, 5, tzinfo=UTC),
        metrics=metrics,
        collection_gaps=gaps,
        policy_digest=policy_digest,
        floor_ratio=value if status == "evaluated" else None,
    )


def _sufficiency(
    *,
    reason: SufficiencyReasonCode | None = None,
    policy_digest: str | None = _SHA,
    error_code: str | None = None,
    integrity: str = "complete",
) -> TraceSufficiencyFacts:
    insufficient = (
        (TraceInsufficientCase.model_validate({"case_id": _CASE, "reason_codes": (reason,)}),)
        if reason is not None
        else ()
    )
    return TraceSufficiencyFacts.model_validate(
        {
            "schema_version": "1",
            "change_id": _CHANGE,
            "authoritative_batch_id": "batch-1",
            "policy_digest": None if error_code is not None else policy_digest,
            "as_of": None if error_code is not None else datetime(2026, 9, 5, tzinfo=UTC),
            "integrity": integrity,
            "integrity_blocks_routing": integrity == "incomplete",
            "sufficient": reason is None and error_code is None,
            "has_open_problems": False,
            "error_code": error_code,
            "insufficient_cases": insufficient,
            "gap_codes": (),
        }
    )


@pytest.mark.parametrize(
    "reason",
    (
        "not_in_current_batch",
        "uncovered",
        "never_run",
        "execution_stale",
        "fuzz_run_missing",
        "perf_run_missing",
        "no_pass",
        "pass_stale",
    ),
)
def test_every_sufficiency_shortfall_requires_case_rework(reason: SufficiencyReasonCode) -> None:
    selected = (
        ("api", "fuzz")
        if reason == "fuzz_run_missing"
        else (("api", "performance") if reason == "perf_run_missing" else ("api",))
    )
    assert (
        classify_coverage_state(
            metrics=_metrics(),
            sufficiency=_sufficiency(reason=reason),
            scope=_scope(selected=selected),
            policy=_policy(),
        )
        == "repair_required"
    )


@pytest.mark.parametrize("status", ("skipped", "not_evaluated", "collection_failed"))
def test_applicable_metric_without_a_measurement_is_inconclusive(status: str) -> None:
    assert (
        classify_coverage_state(
            metrics=_metrics(status=status),
            sufficiency=_sufficiency(),
            scope=_scope(),
            policy=_policy(),
        )
        == "inconclusive"
    )


def test_risk_floor_is_inclusive_and_below_floor_requires_case_rework() -> None:
    assert (
        classify_coverage_state(
            metrics=_metrics(value=0.90),
            sufficiency=_sufficiency(),
            scope=_scope(),
            policy=_policy(),
        )
        == "satisfied"
    )
    assert (
        classify_coverage_state(
            metrics=_metrics(value=0.80),
            sufficiency=_sufficiency(),
            scope=_scope(),
            policy=_policy(),
        )
        == "repair_required"
    )


def test_no_applicable_numeric_goal_can_pass_with_bound_applicability() -> None:
    assert (
        classify_coverage_state(
            metrics=_metrics(status="skipped"),
            sufficiency=_sufficiency(),
            scope=_scope(goals=()),
            policy=_policy(),
        )
        == "satisfied"
    )


@pytest.mark.parametrize(
    ("metrics", "sufficiency", "scope"),
    (
        (_metrics(policy_digest="b" * 64), _sufficiency(), _scope()),
        (_metrics(), _sufficiency(policy_digest="b" * 64), _scope()),
        (_metrics(), _sufficiency(error_code="policy_error"), _scope()),
        (_metrics(), _sufficiency(integrity="incomplete"), _scope()),
        (_metrics(risk_tier="medium"), _sufficiency(), _scope()),
    ),
)
def test_identity_integrity_and_policy_mismatch_are_inconclusive(
    metrics: MetricsDocument,
    sufficiency: TraceSufficiencyFacts,
    scope: ActiveCoverageScopeV1,
) -> None:
    assert (
        classify_coverage_state(
            metrics=metrics,
            sufficiency=sufficiency,
            scope=scope,
            policy=_policy(),
        )
        == "inconclusive"
    )


def _case(*, priority: str, severity: str, declared: str) -> CaseEntryAuthoring:
    return CaseEntryAuthoring.model_validate(
        {
            "case_id": _CASE,
            "title": "authorization",
            "status": "active",
            "priority": priority,
            "severity": severity,
            "type": "API",
            "module": "auth",
            "requirement_id": "REQ-1",
            "feature_name": "authorization",
            "test_condition_id": "COND-1",
            "design_technique": "decision_table",
            "objective": "verify authorization",
            "summary": "exercise authorization",
            "preconditions": [],
            "test_data": [],
            "steps": ["call endpoint"],
            "assertions": ["access is enforced"],
            "postconditions": [],
            "edge_cases": [],
            "related_cases": [],
            "risk": {
                "level": declared,
                "likelihood": 3,
                "impact": 4,
                "rationale": "security boundary",
            },
            "automation": {"required": True, "framework": "pytest", "status": "planned"},
            "regression": {
                "candidate": True,
                "tier": "smoke",
                "rationale": "protect boundary",
                "selection_reason": ["security"],
                "maintenance_rule": "keep",
            },
            "trace": {"auth.session": {"covered": True}},
        }
    )


@pytest.mark.parametrize(
    ("priority", "severity", "declared", "expected_bound", "expected_tier", "lowered"),
    (
        ("P0", "minor", "low", "critical", "critical", True),
        ("P3", "blocker", "low", "critical", "critical", True),
        ("P3", "critical", "low", "high", "high", True),
        ("P1", "minor", "critical", "high", "critical", False),
        ("P2", "major", "medium", "medium", "medium", False),
    ),
)
def test_case_risk_uses_mechanical_priority_and_severity_bounds(
    priority: str,
    severity: str,
    declared: str,
    expected_bound: str,
    expected_tier: str,
    lowered: bool,
) -> None:
    risk = resolve_case_risk((_case(priority=priority, severity=severity, declared=declared),))
    assert risk.lower_bound == expected_bound
    assert risk.tier == expected_tier
    assert risk.declaration_lowered is lowered
