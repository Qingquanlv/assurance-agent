"""Shared literals and dimension sub-models used across artifact models.

Enum values transcribed from the TS source `src/schema/contracts.ts`
(GateStatus, FunctionalCounts, CoverageThreshold, dimensions).
"""

from typing import Annotated, Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field

GateStatus = Literal["PASS", "PASS_WITH_WARNINGS", "FAIL", "SKIPPED"]
ReportRiskLevel = Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]
# The risk band as case authors write it and as the metrics document publishes it
# (`case.yaml` `risk.level`, `metrics.json` `risk_tier`). Distinct from
# `ReportRiskLevel`, which is the uppercase vocabulary the published quality
# report already used; one shared literal here is what lets the metrics document
# take `max(mechanical bound, declared level)` without two vocabularies to map
# between. Declaration order *is* the ordering — `RISK_TIER_ORDER` derives from
# it rather than restating it, so a new band cannot be ranked two ways.
RiskTier = Literal["low", "medium", "high", "critical"]
RISK_TIER_ORDER: tuple[RiskTier, ...] = get_args(RiskTier)
NonEmptyStr = Annotated[str, Field(min_length=1)]
# TS case_yaml.ts: case_id must be underscore-only (hyphens not allowed).
CaseId = Annotated[str, Field(pattern=r"^[A-Za-z0-9_]+$")]


class StrictWireModel(BaseModel):
    """Immutable, non-coercing base for new canonical JSON wire artifacts."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


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
    # Informational only, and deliberately not part of `status`: the dump of an
    # `EvidenceCoverageEvaluation`, reported here so the case-evidence verdict
    # travels with the batch it was computed for. `status` above stays a
    # line/branch judgement, and case sufficiency is routed by the dedicated
    # trace-sufficiency gate rather than by this dimension. Additive and
    # optional so gate documents published before it still load.
    evidence: dict[str, Any] | None = None
