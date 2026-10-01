"""Intake op input and the change marker its Agent writes."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from assurance_intake.contracts.agent import SkillInputV1
from assurance_intake.contracts.common import NonEmptyStr, TestFamily, validate_family_tuple


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
        return validate_family_tuple(value)


class IntakeQaV1(BaseModel):
    """Bootstrap marker, before case-design adds the full change document."""

    model_config = ConfigDict(extra="forbid")

    change_id: NonEmptyStr
