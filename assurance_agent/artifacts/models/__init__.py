"""Public surface of the artifact contract models (spec 4a).

Every module that consumes structured artifacts imports from here.
"""

from assurance_agent.artifacts.models.cases import CaseEntry, CaseRemoval, CaseYaml, QaYaml
from assurance_agent.artifacts.models.common import (
    CoverageDimension,
    CoverageThreshold,
    FunctionalCounts,
    FunctionalDimension,
    GateStatus,
    ReportRiskLevel,
)
from assurance_agent.artifacts.models.execution import ExecutionManifest, SelectedTargets
from assurance_agent.artifacts.models.explore import (
    Advisory,
    FactBaseline,
    FactBaselineFull,
    FactBaselineUnavailable,
)
from assurance_agent.artifacts.models.healing import (
    ApplySummary,
    FixProposal,
    FixProposalItem,
    FixProposalSummary,
    SafetyCheck,
)
from assurance_agent.artifacts.models.inspect import (
    CoverageGapEntry,
    FailureAnalysis,
    FailureCategory,
    FailureEntry,
    FailureEvidence,
    FailureSeverity,
    NonFunctionalDimension,
    PerformanceScenarioVerdict,
    QualityGateDimensions,
    QualityGateResult,
    Reclassified,
)
from assurance_agent.artifacts.models.report import (
    QualityReport,
    QualityScoreBreakdown,
    ReportDefect,
    ReportDefects,
    ReportScope,
)
from assurance_agent.artifacts.models.review import Review, ReviewDecision
from assurance_agent.artifacts.models.state import (
    HealingPhaseState,
    PhaseState,
    RunContext,
    WorkflowGates,
    WorkflowPhases,
    WorkflowState,
)

__all__ = [
    "Advisory",
    "ApplySummary",
    "CaseEntry",
    "CaseRemoval",
    "CaseYaml",
    "CoverageDimension",
    "CoverageGapEntry",
    "CoverageThreshold",
    "ExecutionManifest",
    "FactBaseline",
    "FactBaselineFull",
    "FactBaselineUnavailable",
    "FailureAnalysis",
    "FailureCategory",
    "FailureEntry",
    "FailureEvidence",
    "FailureSeverity",
    "FixProposal",
    "FixProposalItem",
    "FixProposalSummary",
    "FunctionalCounts",
    "FunctionalDimension",
    "GateStatus",
    "HealingPhaseState",
    "NonFunctionalDimension",
    "PerformanceScenarioVerdict",
    "PhaseState",
    "QaYaml",
    "QualityGateDimensions",
    "QualityGateResult",
    "QualityReport",
    "QualityScoreBreakdown",
    "Reclassified",
    "ReportDefect",
    "ReportDefects",
    "ReportRiskLevel",
    "ReportScope",
    "Review",
    "ReviewDecision",
    "RunContext",
    "SafetyCheck",
    "SelectedTargets",
    "WorkflowGates",
    "WorkflowPhases",
    "WorkflowState",
]
