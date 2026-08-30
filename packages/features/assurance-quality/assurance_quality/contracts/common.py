"""Shared quality-owned gate dimensions and frozen-catalog membership checks."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Literal

from pydantic import BaseModel, ValidationInfo

from assurance_intake.contracts import CaseId, NonEmptyStr, RiskTier
from assurance_intake.contracts.common import RISK_TIER_ORDER

GateStatus = Literal["PASS", "PASS_WITH_WARNINGS", "FAIL", "SKIPPED"]
ReportRiskLevel = Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]


class FunctionalCounts(BaseModel):
    total: int
    passed: int
    failed: int


class CoverageThreshold(BaseModel):
    line: float
    branch: float
    module_line: float | None = None
    diff_line: float | None = None


class FunctionalDimension(BaseModel):
    status: GateStatus
    api: FunctionalCounts
    e2e: FunctionalCounts
    fuzz: FunctionalCounts | None = None
    unmapped_tests: int | None = None


class CoverageDimension(BaseModel):
    status: GateStatus
    available: bool
    line_coverage: float
    branch_coverage: float
    threshold: CoverageThreshold
    scope: Any = None
    evidence: dict[str, Any] | None = None


def frozen_catalog(info: ValidationInfo, key: str, *, required: bool = False) -> frozenset[str] | None:
    context = info.context or {}
    if key not in context:
        if required:
            raise ValueError(f"{key} context must be a frozenset of strings")
        return None
    catalog = context.get(key)
    if not isinstance(catalog, frozenset) or any(not isinstance(item, str) for item in catalog):
        raise ValueError(f"{key} context must be a frozenset of strings")
    return catalog


def require_catalog_members(
    catalog: frozenset[str] | None,
    values: Iterable[str | None],
    *,
    error: str,
) -> None:
    if catalog is None:
        return
    for value in values:
        if value is None or value == "":
            continue
        if value not in catalog:
            raise ValueError(error)


__all__ = [
    "CaseId",
    "CoverageDimension",
    "CoverageThreshold",
    "FunctionalCounts",
    "FunctionalDimension",
    "GateStatus",
    "NonEmptyStr",
    "RISK_TIER_ORDER",
    "ReportRiskLevel",
    "RiskTier",
    "frozen_catalog",
    "require_catalog_members",
]
