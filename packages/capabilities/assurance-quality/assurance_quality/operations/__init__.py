from collections.abc import Mapping
from types import MappingProxyType
from typing import cast

from graph_engine.plugin_api import TaskHandler

from assurance_quality.agent_ops.fact_baseline import (
    finalize as fact_baseline_finalize,
    prepare as fact_baseline_prepare,
)
from assurance_quality.agent_ops.inspect import finalize as inspect_finalize, prepare as inspect_prepare
from assurance_quality.agent_ops.issue_analysis import (
    finalize as issue_analysis_finalize,
    prepare as issue_analysis_prepare,
)
from assurance_quality.agent_ops.issue_triage import (
    finalize as issue_triage_finalize,
    prepare as issue_triage_prepare,
)
from assurance_quality.agent_ops.report import finalize as report_finalize, prepare as report_prepare
from assurance_quality.operations.assessment import MaterializeAssessmentHandler
from assurance_quality.operations.coverage import (
    BuildCoverageGapsHandler,
    CollectDiffCoverageHandler,
    ComputeAuthMatrixHandler,
    ComputeConstraintCoverageHandler,
    ComputeJourneyCoverageHandler,
    ComputeThresholdSlackHandler,
    DerivePlanLayerApplicabilityHandler,
    MaterializeCLayerMetricsHandler,
    MaterializeMinimumCoverageHandler,
    MaterializeQuarantineProjectionHandler,
    MaterializeTraceAndCoverageGapsHandler,
    ProbeCoverageRepairNeedHandler,
)
from assurance_quality.operations.inspect import InspectHandler
from assurance_quality.operations.issues import (
    ApplyProblemReviewHandler,
    CollectObservationsHandler,
    LoadProblemReviewContextHandler,
    ReconcileIssuesHandler,
    RecordEmptyIssueAnalysisHandler,
    RecordIssueAnalysisFailureHandler,
    RecordProjectSyncPendingHandler,
)
from assurance_quality.operations.metrics import (
    CollectAdversarialYieldHandler,
    CollectPrMetricsBatchHandler,
    ComputeAssertionStrengthHandler,
    ComputeBaselineDriftHandler,
    LoadLatestPrMetricsHandler,
    MaterializePrMetricsHandler,
    RunMutationSampleHandler,
)
from assurance_quality.operations.nightly import (
    AggregateNightlyMetricsHandler,
    EvaluateRetrospectiveShortboardsHandler,
    RunNightlyMetricsPipelineHandler,
)
from assurance_quality.operations.report import DashboardHandler, GenerateReportHandler
from assurance_quality.operations.surface_baseline import SurfaceBaselineHandler
from assurance_quality.operations.trace import MaterializeTraceHandler


def quality_handlers() -> Mapping[str, TaskHandler]:
    return MappingProxyType(
        {
            "assurance.quality.aggregate-nightly-metrics": AggregateNightlyMetricsHandler(),
            "assurance.quality.apply-problem-review": ApplyProblemReviewHandler(),
            "assurance.quality.build-coverage-gap-signals": BuildCoverageGapsHandler(),
            "assurance.quality.collect-adversarial-yield": CollectAdversarialYieldHandler(),
            "assurance.quality.collect-diff-coverage": CollectDiffCoverageHandler(),
            "assurance.quality.collect-observations": CollectObservationsHandler(),
            "assurance.quality.collect-pr-metrics-batch": CollectPrMetricsBatchHandler(),
            "assurance.quality.compute-assertion-strength": ComputeAssertionStrengthHandler(),
            "assurance.quality.compute-auth-matrix": ComputeAuthMatrixHandler(),
            "assurance.quality.compute-baseline-drift": ComputeBaselineDriftHandler(),
            "assurance.quality.compute-constraint-coverage": ComputeConstraintCoverageHandler(),
            "assurance.quality.compute-journey-coverage": ComputeJourneyCoverageHandler(),
            "assurance.quality.compute-threshold-slack": ComputeThresholdSlackHandler(),
            "assurance.quality.dashboard": DashboardHandler(),
            "assurance.quality.derive-plan-layer-applicability": DerivePlanLayerApplicabilityHandler(),
            "assurance.quality.evaluate-retrospective-shortboards": EvaluateRetrospectiveShortboardsHandler(),
            "assurance.quality.fact-baseline.finalize": cast(TaskHandler, fact_baseline_finalize),
            "assurance.quality.fact-baseline.prepare": cast(TaskHandler, fact_baseline_prepare),
            "assurance.quality.generate-report": GenerateReportHandler(),
            "assurance.quality.inspect": InspectHandler(),
            "assurance.quality.inspect.finalize": cast(TaskHandler, inspect_finalize),
            "assurance.quality.inspect.prepare": cast(TaskHandler, inspect_prepare),
            "assurance.quality.issue-analysis.finalize": cast(TaskHandler, issue_analysis_finalize),
            "assurance.quality.issue-analysis.prepare": cast(TaskHandler, issue_analysis_prepare),
            "assurance.quality.issue-triage.finalize": cast(TaskHandler, issue_triage_finalize),
            "assurance.quality.issue-triage.prepare": cast(TaskHandler, issue_triage_prepare),
            "assurance.quality.load-latest-pr-metrics": LoadLatestPrMetricsHandler(),
            "assurance.quality.load-problem-review-context": LoadProblemReviewContextHandler(),
            "assurance.quality.materialize-assessment-inputs.execute": MaterializeAssessmentHandler(),
            "assurance.quality.materialize-c-layer-metrics": MaterializeCLayerMetricsHandler(),
            "assurance.quality.materialize-minimum-coverage": MaterializeMinimumCoverageHandler(),
            "assurance.quality.materialize-pr-metrics": MaterializePrMetricsHandler(),
            "assurance.quality.materialize-quarantine-projection": MaterializeQuarantineProjectionHandler(),
            "assurance.quality.materialize-trace-and-coverage-gaps": MaterializeTraceAndCoverageGapsHandler(),
            "assurance.quality.materialize-trace-projection": MaterializeTraceHandler(),
            "assurance.quality.probe-coverage-repair-need": ProbeCoverageRepairNeedHandler(),
            "assurance.quality.reconcile-issues.execute": ReconcileIssuesHandler(),
            "assurance.quality.record-empty-issue-analysis": RecordEmptyIssueAnalysisHandler(),
            "assurance.quality.record-issue-analysis-failure": RecordIssueAnalysisFailureHandler(),
            "assurance.quality.record-project-sync-pending": RecordProjectSyncPendingHandler(),
            "assurance.quality.report.finalize": cast(TaskHandler, report_finalize),
            "assurance.quality.report.prepare": cast(TaskHandler, report_prepare),
            "assurance.quality.run-mutation-sample": RunMutationSampleHandler(),
            "assurance.quality.run-nightly-metrics-pipeline": RunNightlyMetricsPipelineHandler(),
            "assurance.quality.surface-baseline.execute": SurfaceBaselineHandler(),
        }
    )


__all__ = [
    "AggregateNightlyMetricsHandler",
    "ApplyProblemReviewHandler",
    "BuildCoverageGapsHandler",
    "CollectAdversarialYieldHandler",
    "CollectDiffCoverageHandler",
    "CollectObservationsHandler",
    "CollectPrMetricsBatchHandler",
    "ComputeAssertionStrengthHandler",
    "ComputeAuthMatrixHandler",
    "ComputeBaselineDriftHandler",
    "ComputeConstraintCoverageHandler",
    "ComputeJourneyCoverageHandler",
    "ComputeThresholdSlackHandler",
    "DashboardHandler",
    "DerivePlanLayerApplicabilityHandler",
    "GenerateReportHandler",
    "InspectHandler",
    "EvaluateRetrospectiveShortboardsHandler",
    "LoadLatestPrMetricsHandler",
    "LoadProblemReviewContextHandler",
    "MaterializeCLayerMetricsHandler",
    "MaterializeAssessmentHandler",
    "MaterializeMinimumCoverageHandler",
    "MaterializePrMetricsHandler",
    "MaterializeQuarantineProjectionHandler",
    "MaterializeTraceAndCoverageGapsHandler",
    "MaterializeTraceHandler",
    "ProbeCoverageRepairNeedHandler",
    "ReconcileIssuesHandler",
    "RecordEmptyIssueAnalysisHandler",
    "RecordIssueAnalysisFailureHandler",
    "RecordProjectSyncPendingHandler",
    "RunMutationSampleHandler",
    "RunNightlyMetricsPipelineHandler",
    "SurfaceBaselineHandler",
    "quality_handlers",
]
