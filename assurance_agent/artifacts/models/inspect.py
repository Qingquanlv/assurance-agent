"""inspect/failure-analysis.json (must_compat) and inspect/quality-gate-result.json
(versioned).

Transcribed from src/schema/failure_analysis.ts, src/schema/quality_gate_result.ts
and the type definitions in src/schema/contracts.ts. Healing gates reference
source_batch_id, failures[].fix_proposal_eligible and final_status verbatim.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from assurance_agent.artifacts.models.common import (
    CoverageDimension,
    FunctionalDimension,
    GateStatus,
)

FailureCategory = Literal[
    "environment_failure",
    "test_data_failure",
    "locator_failure",
    "wait_strategy_failure",
    "assertion_failure",
    "assertion_expectation_error",
    "business_logic_failure",
    "case_semantic_failure",
    "test_code_error",
    "known_product_issue",
    "coverage_gap",
    "fuzz_configuration_error",
    "fuzz_stateful_failure",
    "perf_script_error",
    "perf_threshold_exceeded",
    "perf_environment",
    "manifest_asset_missing",
    "unknown",
]
FailureSeverity = Literal["low", "medium", "high", "critical"]


class FailureEvidence(BaseModel):
    result_file: str
    test_file: str
    trace: str
    screenshot: str
    video: str
    raw_log: str
    log_excerpt: str


class Reclassified(BaseModel):
    # The artifact key "from" is a Python keyword, hence the alias.
    model_config = ConfigDict(populate_by_name=True)

    from_: FailureCategory = Field(alias="from")
    evidence: str
    at: str


class FailureEntry(BaseModel):
    case_id: str
    target: Literal["api", "e2e", "fuzz", "performance", "coverage"]
    category: FailureCategory
    fix_proposal_eligible: bool
    severity: FailureSeverity
    evidence: FailureEvidence
    diagnosis: str
    recommended_action: str
    id: str | None = None
    test: str | None = None
    recommended_next_action: str | None = None
    needs_review: bool | None = None
    reclassified: Reclassified | None = None


class CoverageGapEntry(BaseModel):
    file: str
    line_coverage: float
    threshold: float


class FailureAnalysis(BaseModel):
    schema_version: Literal["1.0"]
    change_id: str
    source_manifest: str
    inspection_status: Literal["completed", "skipped", "failed"]
    batch_id: str
    source_batch_id: str
    final_status: GateStatus
    inspect_mode: Literal["primary", "compat_fallback"]
    compat_fallback_reason: str | None = None
    classification_performed: bool
    status: Literal["analyzed", "no_failures", "skipped", "failed"]
    failures: list[FailureEntry]
    hard_fails: list[FailureEntry]
    needs_review: list[FailureEntry]
    known_product_issues: list[FailureEntry]
    warnings: list[str] | None = None
    coverage_gaps: list[CoverageGapEntry] | None = None


class PerformanceScenarioVerdict(BaseModel):
    capability: str
    endpoint: str
    measured_p95_ms: float | None
    threshold_p95_ms: float
    measured_error_rate: float | None
    threshold_error_rate_max: float
    verdict: Literal["PASS", "FAIL", "SKIPPED"]


class NonFunctionalDimension(BaseModel):
    status: GateStatus
    performance: list[PerformanceScenarioVerdict]


class QualityGateDimensions(BaseModel):
    functional: FunctionalDimension
    coverage: CoverageDimension
    non_functional: NonFunctionalDimension | None = None


class QualityGateResult(BaseModel):
    schema_version: Literal["1.0"]
    change_id: str
    batch_id: str
    dimensions: QualityGateDimensions
    final_status: GateStatus
    warnings: list[str] | None = None
    diagnostics: dict | None = None
