from collections.abc import Mapping
from types import MappingProxyType
from typing import cast

from graph_engine.plugin_api import TaskHandler

from assurance_improvement.agent_ops.archive import finalize as archive_finalize, prepare as archive_prepare
from assurance_improvement.agent_ops.improvement_review import (
    finalize as improvement_review_finalize,
    prepare as improvement_review_prepare,
)
from assurance_improvement.agent_ops.retro import finalize as retro_finalize, prepare as retro_prepare
from assurance_improvement.agent_ops.retro_eval_analysis import (
    finalize as retro_eval_finalize,
    prepare as retro_eval_prepare,
)
from assurance_improvement.agent_ops.retro_issue_analysis import (
    finalize as retro_issue_finalize,
    prepare as retro_issue_prepare,
)
from assurance_improvement.agent_ops.retro_workflow_analysis import (
    finalize as retro_workflow_finalize,
    prepare as retro_workflow_prepare,
)
from assurance_improvement.operations.archive import ProjectArchiveHandler
from assurance_improvement.operations.delivery import (
    ApplyMemoryImprovementHandler,
    EvaluateMemoryImprovementHandler,
    ExportChangeImprovementHandler,
    ExportKnowledgeImprovementHandler,
    LoadImprovementDeliveryHandler,
    RecordChangeImprovementAppliedHandler,
    RecordKnowledgeImprovementAppliedHandler,
    RollbackMemoryImprovementHandler,
)
from assurance_improvement.operations.review import (
    ApplyImprovementAutoReviewHandler,
    ApplyImprovementReviewHandler,
    LoadImprovementReviewContextHandler,
    LoadReviewSubjectHandler,
    RecordAutoReviewOrchestrationErrorHandler,
    RecordImprovementAutoReviewErrorHandler,
    SelectCurrentRetroAutoReviewItemsHandler,
    SummarizeAutoReviewBatchHandler,
    ValidateImprovementReviewAssessmentHandler,
)
from assurance_improvement.operations.retro import (
    AssembleRetroContextHandler,
    DrainImprovementOutboxHandler,
    FinalizeRetroStatusHandler,
    MaterializeEmptyRetroAnalysisHandler,
    ReconcileImprovementsHandler,
    RecordAnalysisFailedHandler,
    RecordRetroPipelineFailureHandler,
    RetroCollectHandler,
    RetroEvidenceGapFallbackHandler,
)
from assurance_improvement.operations.retro_slices import RetroBuildSlicesHandler


def improvement_handlers() -> Mapping[str, TaskHandler]:
    return MappingProxyType(
        {
            "assurance.improvement.retro-build-slices.execute": RetroBuildSlicesHandler(),
            "assurance.improvement.apply-improvement-auto-review": ApplyImprovementAutoReviewHandler(),
            "assurance.improvement.apply-improvement-review": ApplyImprovementReviewHandler(),
            "assurance.improvement.apply-memory-improvement": ApplyMemoryImprovementHandler(),
            "assurance.improvement.archive.finalize": cast(TaskHandler, archive_finalize),
            "assurance.improvement.archive.prepare": cast(TaskHandler, archive_prepare),
            "assurance.improvement.assemble-retro-context-v3": AssembleRetroContextHandler(),
            "assurance.improvement.drain-improvement-outbox": DrainImprovementOutboxHandler(),
            "assurance.improvement.evaluate-memory-improvement": EvaluateMemoryImprovementHandler(),
            "assurance.improvement.export-change-improvement": ExportChangeImprovementHandler(),
            "assurance.improvement.export-knowledge-improvement": ExportKnowledgeImprovementHandler(),
            "assurance.improvement.finalize-retro-status": FinalizeRetroStatusHandler(),
            "assurance.improvement.improvement-review.finalize": cast(
                TaskHandler, improvement_review_finalize
            ),
            "assurance.improvement.improvement-review.prepare": cast(TaskHandler, improvement_review_prepare),
            "assurance.improvement.load-improvement-delivery": LoadImprovementDeliveryHandler(),
            "assurance.improvement.load-improvement-review-context": LoadImprovementReviewContextHandler(),
            "assurance.improvement.load-review-subject": LoadReviewSubjectHandler(),
            "assurance.improvement.materialize-empty-retro-analysis": MaterializeEmptyRetroAnalysisHandler(),
            "assurance.improvement.project-archive": ProjectArchiveHandler(),
            "assurance.improvement.reconcile-improvements": ReconcileImprovementsHandler(),
            "assurance.improvement.record-analysis-failed": RecordAnalysisFailedHandler(),
            "assurance.improvement.record-auto-review-orchestration-error": (
                RecordAutoReviewOrchestrationErrorHandler()
            ),
            "assurance.improvement.record-change-improvement-applied": RecordChangeImprovementAppliedHandler(),
            "assurance.improvement.record-improvement-auto-review-error": (
                RecordImprovementAutoReviewErrorHandler()
            ),
            "assurance.improvement.record-knowledge-improvement-applied": (
                RecordKnowledgeImprovementAppliedHandler()
            ),
            "assurance.improvement.record-retro-pipeline-failure": RecordRetroPipelineFailureHandler(),
            "assurance.improvement.retro-collect-v3": RetroCollectHandler(),
            "assurance.improvement.retro-eval-analysis.finalize": cast(TaskHandler, retro_eval_finalize),
            "assurance.improvement.retro-eval-analysis.prepare": cast(TaskHandler, retro_eval_prepare),
            "assurance.improvement.retro-evidence-gap-fallback": RetroEvidenceGapFallbackHandler(),
            "assurance.improvement.retro-issue-analysis.finalize": cast(TaskHandler, retro_issue_finalize),
            "assurance.improvement.retro-issue-analysis.prepare": cast(TaskHandler, retro_issue_prepare),
            "assurance.improvement.retro-workflow-analysis.finalize": cast(
                TaskHandler, retro_workflow_finalize
            ),
            "assurance.improvement.retro-workflow-analysis.prepare": cast(
                TaskHandler, retro_workflow_prepare
            ),
            "assurance.improvement.retro.finalize": cast(TaskHandler, retro_finalize),
            "assurance.improvement.retro.prepare": cast(TaskHandler, retro_prepare),
            "assurance.improvement.rollback-memory-improvement": RollbackMemoryImprovementHandler(),
            "assurance.improvement.select-current-retro-auto-review-items": (
                SelectCurrentRetroAutoReviewItemsHandler()
            ),
            "assurance.improvement.summarize-auto-review-batch": SummarizeAutoReviewBatchHandler(),
            "assurance.improvement.validate-improvement-review-assessment": (
                ValidateImprovementReviewAssessmentHandler()
            ),
        }
    )


__all__ = [
    "improvement_handlers",
]
