"""Closed coverage goals and the active Case-derived assessment scope."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Literal, Self

from pydantic import Field, field_validator, model_validator

from graph_engine.plugin_api import FrozenModel

from assurance_intake.contracts import RiskTier
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1

FiniteFloor = Annotated[float, Field(ge=0.0, le=1.0, allow_inf_nan=False)]
CoverageGoal = Literal["constraint_coverage", "auth_matrix_coverage", "journey_coverage"]
SelectedFamily = Literal["api", "e2e", "fuzz", "performance"]

_GOAL_ORDER: tuple[CoverageGoal, ...] = (
    "constraint_coverage",
    "auth_matrix_coverage",
    "journey_coverage",
)
_FAMILY_ORDER: tuple[SelectedFamily, ...] = ("api", "e2e", "fuzz", "performance")


class CoverageFloorsV1(FrozenModel):
    low: FiniteFloor
    medium: FiniteFloor
    high: FiniteFloor
    critical: FiniteFloor


class CoverageGoalPolicyV1(FrozenModel):
    coverage_floor_by_tier: CoverageFloorsV1

    @classmethod
    def from_product_policy(cls, product_policy: Mapping[str, object]) -> CoverageGoalPolicyV1:
        raw = product_policy.get("coverage_floor_by_tier")
        if raw is None:
            raise ValueError("policy_error: coverage_floor_by_tier is required")
        return cls.model_validate({"coverage_floor_by_tier": raw})

    def floor_for(self, tier: RiskTier) -> float:
        return float(getattr(self.coverage_floor_by_tier, tier))


class SufficiencyPolicyV1(FrozenModel):
    recency_hours: int = Field(gt=0)
    require_current_batch: Literal[True]

    @classmethod
    def from_product_policy(cls, product_policy: Mapping[str, object]) -> SufficiencyPolicyV1:
        raw = product_policy.get("evidence_sufficiency")
        if not isinstance(raw, Mapping):
            raise ValueError("policy_error: evidence_sufficiency is required")
        return cls.model_validate(raw)


class ActiveCoverageScopeV1(FrozenModel):
    change_id: str = Field(min_length=1)
    coverage_epoch: int = Field(ge=0)
    required_case_ids: tuple[str, ...] = Field(min_length=1)
    selected_families: tuple[SelectedFamily, ...]
    applicable_goals: tuple[CoverageGoal, ...]
    applicability_refs: tuple[EvidenceArtifactRefV1, ...] = Field(min_length=1)
    risk_tier: RiskTier
    policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("required_case_ids")
    @classmethod
    def _case_ids_are_canonical(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item or item != item.strip() for item in value):
            raise ValueError("required_case_ids must be non-empty canonical strings")
        if value != tuple(sorted(set(value))):
            raise ValueError("required_case_ids must be sorted and unique")
        return value

    @field_validator("selected_families")
    @classmethod
    def _families_are_canonical(
        cls, value: tuple[SelectedFamily, ...]
    ) -> tuple[SelectedFamily, ...]:
        rank = {name: index for index, name in enumerate(_FAMILY_ORDER)}
        if value != tuple(sorted(set(value), key=rank.__getitem__)):
            raise ValueError("selected_families must be sorted and unique")
        return value

    @field_validator("applicable_goals")
    @classmethod
    def _goals_are_canonical(cls, value: tuple[CoverageGoal, ...]) -> tuple[CoverageGoal, ...]:
        rank = {name: index for index, name in enumerate(_GOAL_ORDER)}
        if value != tuple(sorted(set(value), key=rank.__getitem__)):
            raise ValueError("applicable_goals must be sorted and unique")
        return value

    @model_validator(mode="after")
    def _applicability_refs_are_canonical(self) -> Self:
        ordered = tuple(sorted(self.applicability_refs, key=lambda item: (item.path, item.digest)))
        if self.applicability_refs != ordered or len(set(self.applicability_refs)) != len(
            self.applicability_refs
        ):
            raise ValueError("applicability_refs must be sorted and unique")
        return self


__all__ = [
    "ActiveCoverageScopeV1",
    "CoverageFloorsV1",
    "CoverageGoal",
    "CoverageGoalPolicyV1",
    "FiniteFloor",
    "SelectedFamily",
    "SufficiencyPolicyV1",
]
