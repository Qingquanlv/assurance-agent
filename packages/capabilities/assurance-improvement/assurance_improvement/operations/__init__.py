from collections.abc import Mapping
from types import MappingProxyType
from typing import cast

from graph_engine.plugin_api import TaskHandler

from assurance_improvement.operations.delivery import (
    ApplyMemoryImprovementHandler,
    EvaluateMemoryImprovementHandler,
    ExportChangeImprovementHandler,
    RollbackMemoryImprovementHandler,
)
from assurance_improvement.operations.retro import (
    ReconcileImprovementsHandler,
    RetroCollectHandler,
    RetroSynthesizeHandler,
)
from assurance_improvement.operations.retro_slices import RetroBuildSlicesHandler
from assurance_improvement.operations.runtime_snapshot import RetroRuntimeSnapshotHandler
from assurance_improvement.operations.review import (
    ApplyImprovementAutoReviewHandler,
    ApplyImprovementReviewHandler,
)


def improvement_handlers() -> Mapping[str, TaskHandler]:
    from assurance_improvement import ops

    return MappingProxyType(
        {
            **ops.router.handlers(cast(TaskHandler, ops)),
            "assurance.improvement.retro-build-slices.execute": RetroBuildSlicesHandler(),
            "assurance.improvement.apply-improvement-auto-review": ApplyImprovementAutoReviewHandler(),
            "assurance.improvement.apply-improvement-review": ApplyImprovementReviewHandler(),
            "assurance.improvement.apply-memory-improvement": ApplyMemoryImprovementHandler(),
            "assurance.improvement.evaluate-memory-improvement": EvaluateMemoryImprovementHandler(),
            "assurance.improvement.export-change-improvement": ExportChangeImprovementHandler(),
            "assurance.improvement.reconcile-improvements": ReconcileImprovementsHandler(),
            "assurance.improvement.retro-synthesize": RetroSynthesizeHandler(),
            "assurance.improvement.retro-collect-v3": RetroCollectHandler(),
            "assurance.improvement.retro-runtime-snapshot": RetroRuntimeSnapshotHandler(),
            "assurance.improvement.rollback-memory-improvement": RollbackMemoryImprovementHandler(),
        }
    )


__all__ = ["improvement_handlers"]
