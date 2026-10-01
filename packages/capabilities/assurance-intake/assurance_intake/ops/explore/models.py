"""Explore op input."""

from __future__ import annotations

from pydantic import Field, field_validator

from assurance_intake.contracts.common import TestFamily
from assurance_intake.domain.inputs import SkillInputV1, canonical_test_families


class ExploreInputV1(SkillInputV1):
    candidate_test_families: tuple[TestFamily, ...] = Field(min_length=1)

    @field_validator("candidate_test_families")
    @classmethod
    def _candidate_test_families(cls, value: tuple[TestFamily, ...]) -> tuple[TestFamily, ...]:
        return canonical_test_families(value)
