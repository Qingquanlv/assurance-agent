"""inspect/failure-analysis.json (must_compat) and inspect/quality-gate-result.json
(versioned).

Transcribed from src/schema/failure_analysis.ts, src/schema/quality_gate_result.ts
and the type definitions in src/schema/contracts.ts. Healing gates reference
source_batch_id, failures[].fix_proposal_eligible and final_status verbatim.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, RootModel, model_validator

from assurance_kernel.artifacts.models.common import (
    CoverageDimension,
    CoverageThreshold,
    FunctionalDimension,
    GateStatus,
)
from assurance_kernel.artifacts.models.sufficiency import SufficiencyReportV2

_FROZEN = ConfigDict(frozen=True, extra="forbid")

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


class MetricsDimension(BaseModel):
    """Informational verification-metrics summary for report/retro.

    Never consulted by ``worst_status`` / ``final_status``, healing-loop reject,
    or archive stop. Routing lives on ``metrics-sufficiency-gate``.
    """

    status: GateStatus
    available: bool = True
    summary: str | None = None


class QualityGateDimensions(BaseModel):
    functional: FunctionalDimension
    coverage: CoverageDimension
    non_functional: NonFunctionalDimension | None = None
    # Pure report channel. Optional so older gate documents still load.
    metrics: MetricsDimension | None = None


class QualityGateResultV1(BaseModel):
    """The gate verdict, plus observations that deliberately do not decide it.

    ``diagnostics`` is a shadow channel: nothing in it may be read back to
    change ``final_status``, the execution manifest's ``final_status``, or
    healing/archive routing.

    ``dimensions.metrics`` is likewise informational only — see MetricsDimension.
    """

    schema_version: Literal["1.0"]
    change_id: str
    batch_id: str
    dimensions: QualityGateDimensions
    final_status: GateStatus
    warnings: list[str] | None = None
    diagnostics: dict[str, Any] | None = None


QualityGateResult = QualityGateResultV1


class EvidenceCoverageSuccessV2(BaseModel):
    model_config = _FROZEN
    kind: Literal["sufficiency"]
    report: SufficiencyReportV2

    @model_validator(mode="after")
    def _require_current_batch(self) -> Self:
        if self.report.require_current_batch is not True:
            raise ValueError("quality v2 sufficiency must require current batch")
        return self


class EvidenceCoverageErrorV2(BaseModel):
    model_config = _FROZEN
    kind: Literal["error"]
    error_code: Literal["evidence_projection_missing", "policy_error"]


EvidenceCoveragePayloadV2 = Annotated[
    EvidenceCoverageSuccessV2 | EvidenceCoverageErrorV2,
    Field(discriminator="kind"),
]


class CoverageDimensionV2(BaseModel):
    model_config = _FROZEN
    status: GateStatus
    available: bool
    line_coverage: float
    branch_coverage: float
    threshold: CoverageThreshold
    scope: Any = None
    evidence: EvidenceCoveragePayloadV2


class QualityGateDimensionsV2(BaseModel):
    model_config = _FROZEN
    functional: FunctionalDimension
    coverage: CoverageDimensionV2
    non_functional: NonFunctionalDimension | None = None


class QualityGateResultV2(BaseModel):
    model_config = _FROZEN
    schema_version: Literal["2.0"] = "2.0"
    change_id: str
    batch_id: str
    dimensions: QualityGateDimensionsV2
    final_status: GateStatus
    warnings: list[str] | None = None
    diagnostics: dict | None = None


QualityGateResultLike = QualityGateResultV1 | QualityGateResultV2

QualityGateResultVariant = Annotated[
    QualityGateResultV1 | QualityGateResultV2,
    Field(discriminator="schema_version"),
]


class QualityGateResultDocument(RootModel[QualityGateResultVariant]):
    pass


def load_quality_gate_result_document(raw: object) -> QualityGateResultV1 | QualityGateResultV2:
    return QualityGateResultDocument.model_validate(raw).root
