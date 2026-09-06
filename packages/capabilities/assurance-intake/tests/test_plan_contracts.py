from __future__ import annotations

import pytest
from pydantic import ValidationError

from assurance_intake.contracts.common import TestFamily
from assurance_intake.contracts.plan import (
    PlanBudgetsV1,
    PreparedQualityGoalV1,
    TestFamilyPolicyV1 as FamilyPolicy,
)
from assurance_intake.contracts.quality_goals import (
    CoverageFloorsV1,
    CoverageGoalPolicyV1,
    SufficiencyPolicyV1,
)
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1


_SHA = "a" * 64


@pytest.mark.parametrize("allowed", [("e2e", "api"), ("api", "api"), ()])
def test_family_policy_rejects_noncanonical_or_empty_allowed(
    allowed: tuple[TestFamily, ...],
) -> None:
    with pytest.raises(ValidationError):
        FamilyPolicy(required=(), allowed=allowed)


def test_family_policy_requires_required_families_to_be_allowed() -> None:
    with pytest.raises(ValidationError, match="required"):
        FamilyPolicy(required=("e2e",), allowed=("api",))


def test_plan_budgets_reject_mutable_usage_or_negative_limits() -> None:
    with pytest.raises(ValidationError):
        PlanBudgetsV1(
            review_rounds=-1,
            coverage_rounds=2,
            healing_rounds=1,
            execution_retries=0,
        )
    with pytest.raises(ValidationError):
        PlanBudgetsV1.model_validate(
            {
                "review_rounds": 1,
                "coverage_rounds": 2,
                "healing_rounds": 1,
                "execution_retries": 0,
                "coverage_rounds_used": 1,
            }
        )


def test_prepared_goal_is_a_definition_without_observed_outcomes() -> None:
    payload = {
        "obligations_ref": EvidenceArtifactRefV1(
            path="qa/changes/CH-1/explore/exploration.json",
            digest=_SHA,
        ),
        "source_resource_digests": (
            ("assurance.product.configuration.capability-catalog", _SHA),
            ("assurance.product.configuration.data-knowledge", "b" * 64),
        ),
        "required_test_families": ("api", "e2e"),
        "metric_catalog": (
            "constraint_coverage",
            "auth_matrix_coverage",
            "journey_coverage",
        ),
        "coverage_policy": CoverageGoalPolicyV1(
            coverage_floor_by_tier=CoverageFloorsV1(
                low=0.7,
                medium=0.8,
                high=0.9,
                critical=1.0,
            )
        ),
        "sufficiency_policy": SufficiencyPolicyV1(
            recency_hours=24,
            require_current_batch=True,
        ),
    }
    goal = PreparedQualityGoalV1.model_validate(payload)
    assert goal.required_test_families == ("api", "e2e")

    with pytest.raises(ValidationError):
        PreparedQualityGoalV1.model_validate({**payload, "observed_value": 1.0})


@pytest.mark.parametrize(
    "source_resource_digests",
    [
        (("assurance.product.configuration.data-knowledge", _SHA),) * 2,
        (
            ("assurance.product.configuration.data-knowledge", _SHA),
            ("assurance.product.configuration.capability-catalog", _SHA),
        ),
        (("unqualified", _SHA),),
        (("assurance.product.configuration.data-knowledge", "A" * 64),),
    ],
)
def test_prepared_goal_rejects_noncanonical_resource_identities(
    source_resource_digests: tuple[tuple[str, str], ...],
) -> None:
    with pytest.raises(ValidationError):
        PreparedQualityGoalV1.model_validate(
            {
                "obligations_ref": {
                    "path": "qa/changes/CH-1/explore/exploration.json",
                    "digest": _SHA,
                },
                "source_resource_digests": source_resource_digests,
                "required_test_families": (),
                "metric_catalog": (
                    "constraint_coverage",
                    "auth_matrix_coverage",
                    "journey_coverage",
                ),
                "coverage_policy": {
                    "coverage_floor_by_tier": {
                        "low": 0.7,
                        "medium": 0.8,
                        "high": 0.9,
                        "critical": 1.0,
                    }
                },
                "sufficiency_policy": {
                    "recency_hours": 24,
                    "require_current_batch": True,
                },
            }
        )
