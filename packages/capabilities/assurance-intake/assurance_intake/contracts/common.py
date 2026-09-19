"""Shared intake contract primitives."""

from __future__ import annotations

from typing import Annotated, Literal, get_args

from pydantic import Field

NonEmptyStr = Annotated[str, Field(min_length=1)]
CaseId = Annotated[str, Field(pattern=r"^[A-Za-z0-9_]+$")]
RiskTier = Literal["low", "medium", "high", "critical"]
RISK_TIER_ORDER: tuple[RiskTier, ...] = get_args(RiskTier)
TestFamily = Literal["api", "e2e", "fuzz", "performance"]
TEST_FAMILY_ORDER: tuple[TestFamily, ...] = get_args(TestFamily)
MrcCategory = Literal["api", "e2e", "e2e_if_enabled", "negative", "data_integrity"]
MrcLayer = Literal["api", "e2e", "both"]


def validate_family_tuple(value: tuple[TestFamily, ...]) -> tuple[TestFamily, ...]:
    expected = tuple(family for family in TEST_FAMILY_ORDER if family in value)
    if value != expected:
        raise ValueError("families must be unique and in canonical order")
    return value
