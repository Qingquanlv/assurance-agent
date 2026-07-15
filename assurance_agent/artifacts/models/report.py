"""report/quality-report.json — written by `aa report generate` (versioned).

Transcribed from src/schema/quality_report.ts / src/schema/contracts.ts.
"""
from typing import Any, Literal

from pydantic import BaseModel

from assurance_agent.artifacts.models.common import (
    CoverageDimension,
    FunctionalDimension,
    GateStatus,
    ReportRiskLevel,
)

ScoreValue = float | Literal["N/A"]


class QualityScoreBreakdown(BaseModel):
    functional: ScoreValue
    coverage: ScoreValue
    fuzz: ScoreValue
    performance: ScoreValue


class ReportScope(BaseModel):
    cases: int
    requirements: list[str]


class ReportDefect(BaseModel):
    case_id: str
    category: str
    diagnosis: str


class ReportDefects(BaseModel):
    product: list[ReportDefect]
    test: list[ReportDefect]
    environment: list[ReportDefect]


class QualityReport(BaseModel):
    schema_version: Literal["1.0"]
    change_id: str
    batch_id: str
    final_status: GateStatus
    quality_score: float
    score_breakdown: QualityScoreBreakdown
    scope: ReportScope
    functional: FunctionalDimension
    coverage: CoverageDimension
    defects: ReportDefects
    risk_level: ReportRiskLevel
    risk_rationale: str
    recommendation: str
    human_decisions: list[Any] | None = None
    minimum_required_coverage: Any = None
    non_functional: Any = None
