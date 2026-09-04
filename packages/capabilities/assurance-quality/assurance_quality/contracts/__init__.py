from __future__ import annotations

from assurance_quality.contracts.attempts import AGENT_JOB_CONTRACTS, attempt_contract_refs
from assurance_quality.contracts.decisions import (
    COVERAGE_STATES,
    CoverageAssessmentPublicV1,
    CoverageState,
    FIX_ELIGIBLE_CLASSIFICATIONS,
    FailureClassification,
    IssueAnalysisPublicV1,
)
from assurance_quality.contracts.baseline import (
    FactBaseline,
    FactBaselineAuthoring,
    FactBaselineFull,
    FactBaselineUnavailable,
)
from assurance_quality.contracts.c_layer import CLayerMetricEntry, CLayerMetricsDocument
from assurance_quality.contracts.coverage import (
    CoverageGap,
    CoverageGapLocator,
    CoverageGapsDocument,
    MinimumCoverageItem,
    MinimumCoverageMatrix,
    MinimumCoverageResult,
    MinimumCoverageSummary,
)
from assurance_quality.contracts.inspect import (
    FailureAnalysis,
    QualityGateResult,
    QualityGateResultDocument,
    QualityGateResultV1,
    QualityGateResultV2,
    load_quality_gate_result_document,
)
from assurance_quality.contracts.issue_events import (
    CHANGE_ISSUE_EVENT_ADAPTER,
    PROBLEM_EVENT_ADAPTER,
    ChangeIssueEvent,
    ProblemEvent,
)
from assurance_quality.contracts.issues import (
    ChangeIssueSnapshot,
    IssueAnalysisStatus,
    IssueCandidateDocument,
    IssueOccurrence,
    Observation,
    ObservationDocument,
    Problem,
    ProblemProjection,
)
from assurance_quality.contracts.metrics import (
    MetricCollectionGap,
    MetricEntry,
    MetricScope,
    MetricShortboard,
    MetricsDocument,
)
from assurance_quality.contracts.pr_metrics import (
    AdversarialYieldEvidence,
    AssertionStrengthEvidence,
    AuthMatrixEvidence,
    BaselineDriftEvidence,
    ConstraintCoverageEvidence,
    CoverageDiffEvidence,
    JourneyCoverageEvidence,
    MutationEvidence,
    PerfSlackEvidence,
)
from assurance_quality.contracts.quarantine import QuarantineEntry, QuarantineProjection
from assurance_quality.contracts.report import QualityReport
from assurance_quality.contracts.sufficiency import (
    SufficiencyReportV2,
    TraceSufficiencyFacts,
)
from assurance_quality.contracts.trace import (
    TraceProjection,
    TraceProjectionDocument,
    TraceProjectionV2,
    load_trace_projection_document,
)

__all__ = [
    "AGENT_JOB_CONTRACTS",
    "COVERAGE_STATES",
    "CoverageAssessmentPublicV1",
    "CoverageState",
    "FIX_ELIGIBLE_CLASSIFICATIONS",
    "FailureClassification",
    "IssueAnalysisPublicV1",
    "CHANGE_ISSUE_EVENT_ADAPTER",
    "PROBLEM_EVENT_ADAPTER",
    "AdversarialYieldEvidence",
    "AssertionStrengthEvidence",
    "AuthMatrixEvidence",
    "BaselineDriftEvidence",
    "CLayerMetricEntry",
    "CLayerMetricsDocument",
    "ChangeIssueEvent",
    "ChangeIssueSnapshot",
    "ConstraintCoverageEvidence",
    "CoverageDiffEvidence",
    "CoverageGap",
    "CoverageGapLocator",
    "CoverageGapsDocument",
    "FactBaseline",
    "FactBaselineAuthoring",
    "FactBaselineFull",
    "FactBaselineUnavailable",
    "FailureAnalysis",
    "IssueAnalysisStatus",
    "IssueCandidateDocument",
    "IssueOccurrence",
    "JourneyCoverageEvidence",
    "MetricCollectionGap",
    "MetricEntry",
    "MetricScope",
    "MetricShortboard",
    "MetricsDocument",
    "MinimumCoverageItem",
    "MinimumCoverageMatrix",
    "MinimumCoverageResult",
    "MinimumCoverageSummary",
    "MutationEvidence",
    "Observation",
    "ObservationDocument",
    "PerfSlackEvidence",
    "Problem",
    "ProblemEvent",
    "ProblemProjection",
    "QualityGateResult",
    "QualityGateResultDocument",
    "QualityGateResultV1",
    "QualityGateResultV2",
    "QualityReport",
    "QuarantineEntry",
    "QuarantineProjection",
    "SufficiencyReportV2",
    "TraceProjection",
    "TraceProjectionDocument",
    "TraceProjectionV2",
    "TraceSufficiencyFacts",
    "attempt_contract_refs",
    "load_quality_gate_result_document",
    "load_trace_projection_document",
]
