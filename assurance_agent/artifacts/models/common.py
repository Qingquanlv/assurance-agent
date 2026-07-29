"""Shared literals and dimension sub-models used across artifact models.

Enum values transcribed from the TS source `src/schema/contracts.ts`
(GateStatus, FunctionalCounts, CoverageThreshold, dimensions).
"""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

GateStatus = Literal["PASS", "PASS_WITH_WARNINGS", "FAIL", "SKIPPED"]
ReportRiskLevel = Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]
NonEmptyStr = Annotated[str, Field(min_length=1)]
# TS case_yaml.ts: case_id must be underscore-only (hyphens not allowed).
CaseId = Annotated[str, Field(pattern=r"^[A-Za-z0-9_]+$")]


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
    evidence: dict | None = None
