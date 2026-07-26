"""report/quality-report.json — written by `aa report generate` (versioned).

Transcribed from src/schema/quality_report.ts / src/schema/contracts.ts.

Schema 1.1 adds an ``issues`` section that carries Issue risk separately from
execution ``final_status``. A historical 1.0 report (no ``issues`` field) is
accepted without any legacy Issue-file lookup.
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

# Issue risk is independent of execution final_status and never overwrites it.
IssueRisk = Literal["unknown", "critical", "high", "medium", "low", "clear"]


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


class IssueReport(BaseModel):
    """Embedded Issue section — schema 1.1 only.

    Carries analysis status, aggregate counts, and a risk level derived
    from the Change Issue snapshot and Project Problem projection.  The
    ``issue_risk`` field is the only cross-cutting signal; it is *never*
    copied into ``final_status``.
    """

    analysis_status: str
    project_sync_status: str
    total_occurrences: int
    counts_by_status: dict[str, int]
    counts_by_classification: dict[str, int]
    counts_by_severity: dict[str, int]
    new_count: int
    repeated_count: int
    regressed_count: int
    resolved_count: int
    accepted_risk_count: int
    not_an_issue_count: int
    issue_risk: IssueRisk
    issue_risk_rationale: str


class QualityReport(BaseModel):
    schema_version: Literal["1.0", "1.1"]
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
    # Execution timing for the scored batch (from events.jsonl). Absent → "No data".
    started_at: str | None = None
    duration: str | None = None
    human_decisions: list[Any] | None = None
    minimum_required_coverage: Any = None
    non_functional: Any = None
    # Issue risk section (schema 1.1). Absent in historical 1.0 reports.
    issues: IssueReport | None = None
