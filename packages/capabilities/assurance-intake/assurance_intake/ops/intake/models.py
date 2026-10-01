"""Intake op input."""

from __future__ import annotations

from pydantic import Field, field_validator

from assurance_intake.contracts.common import TestFamily
from assurance_intake.domain.inputs import SkillInputV1, canonical_test_families


class IntakeInputV1(SkillInputV1):
    requirement: str = Field(min_length=1)
    candidate_test_families: tuple[TestFamily, ...] = ()

    @field_validator("requirement")
    @classmethod
    def _requirement(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("requirement must be a non-empty string")
        return value

    @field_validator("candidate_test_families")
    @classmethod
    def _candidate_test_families(cls, value: tuple[TestFamily, ...]) -> tuple[TestFamily, ...]:
        return canonical_test_families(value)
