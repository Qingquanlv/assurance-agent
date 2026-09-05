"""Closed coverage goals and the active Case-derived assessment scope."""

from __future__ import annotations

from typing import Self

from pydantic import Field, field_validator, model_validator

from graph_engine.plugin_api import FrozenModel

from assurance_intake.contracts import RiskTier
from assurance_intake.contracts.common import TEST_FAMILY_ORDER, TestFamily
from assurance_intake.contracts.quality_goals import (
    COVERAGE_GOAL_ORDER,
    CoverageFloorsV1,
    CoverageGoal,
    CoverageGoalPolicyV1,
    FiniteFloor,
    SufficiencyPolicyV1,
)
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1

SelectedFamily = TestFamily


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
    def _families_are_canonical(cls, value: tuple[SelectedFamily, ...]) -> tuple[SelectedFamily, ...]:
        rank = {name: index for index, name in enumerate(TEST_FAMILY_ORDER)}
        if value != tuple(sorted(set(value), key=rank.__getitem__)):
            raise ValueError("selected_families must be sorted and unique")
        return value

    @field_validator("applicable_goals")
    @classmethod
    def _goals_are_canonical(cls, value: tuple[CoverageGoal, ...]) -> tuple[CoverageGoal, ...]:
        rank = {name: index for index, name in enumerate(COVERAGE_GOAL_ORDER)}
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
