"""Shared intake contract primitives."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Annotated, Literal, get_args

from pydantic import Field

SHA256_PATTERN = r"^[0-9a-f]{64}$"
NonEmptyStr = Annotated[str, Field(min_length=1)]
CaseId = Annotated[str, Field(pattern=r"^[A-Za-z0-9_]+$")]
RiskTier = Literal["low", "medium", "high", "critical"]
RISK_TIER_ORDER: tuple[RiskTier, ...] = get_args(RiskTier)
TestFamily = Literal["api", "e2e", "fuzz", "performance"]
TEST_FAMILY_ORDER: tuple[TestFamily, ...] = get_args(TestFamily)
MrcCategory = Literal["api", "e2e", "e2e_if_enabled", "negative", "data_integrity"]
MrcLayer = Literal["api", "e2e", "both"]


def is_canonical_relative(path: str) -> bool:
    posix = PurePosixPath(path)
    return not (
        posix.is_absolute()
        or "\\" in path
        or (len(path) >= 2 and path[1] == ":")
        or posix.as_posix() != path
        or any(part in {"", ".", ".."} for part in posix.parts)
    )


def require_unique(values: tuple[str, ...], label: str) -> tuple[str, ...]:
    if len(set(values)) != len(values):
        duplicates = sorted({value for value in values if values.count(value) > 1})
        raise ValueError(f"{label} must be unique: {duplicates}")
    return values


def validate_family_tuple(value: tuple[TestFamily, ...]) -> tuple[TestFamily, ...]:
    expected = tuple(family for family in TEST_FAMILY_ORDER if family in value)
    if value != expected:
        raise ValueError("families must be unique and in canonical order")
    return value
