from collections.abc import Mapping
from types import MappingProxyType
from typing import cast

from graph_engine.plugin_api import TaskHandler

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
    from assurance_improvement import ops

    return MappingProxyType(
        {
            **ops.router.handlers(cast(TaskHandler, ops)),
            "assurance.improvement.retro-build-slices.execute": RetroBuildSlicesHandler(),
            "assurance.improvement.apply-improvement-auto-review": ApplyImprovementAutoReviewHandler(),
            "assurance.improvement.apply-improvement-review": ApplyImprovementReviewHandler(),
            "assurance.improvement.apply-memory-improvement": ApplyMemoryImprovementHandler(),
            "assurance.improvement.assemble-retro-context-v3": AssembleRetroContextHandler(),
            "assurance.improvement.drain-improvement-outbox": DrainImprovementOutboxHandler(),
            "assurance.improvement.evaluate-memory-improvement": EvaluateMemoryImprovementHandler(),
            "assurance.improvement.export-change-improvement": ExportChangeImprovementHandler(),
            "assurance.improvement.export-knowledge-improvement": ExportKnowledgeImprovementHandler(),
            "assurance.improvement.finalize-retro-status": FinalizeRetroStatusHandler(),
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
            "assurance.improvement.retro-evidence-gap-fallback": RetroEvidenceGapFallbackHandler(),
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
