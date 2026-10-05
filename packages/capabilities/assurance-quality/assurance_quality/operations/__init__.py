from collections.abc import Mapping
from types import MappingProxyType
from typing import cast

from graph_engine.plugin_api import TaskHandler

from assurance_quality.operations.assessment import MaterializeAssessmentHandler
from assurance_quality.operations.issues import ReconcileIssuesHandler
from assurance_quality.operations.surface_baseline import SurfaceBaselineHandler


def quality_handlers() -> Mapping[str, TaskHandler]:
    from assurance_quality import ops

    declared = dict(ops.router.handlers(cast(TaskHandler, ops)))
    return MappingProxyType(
        {
            **declared,
            "assurance.quality.materialize-assessment-inputs.execute": MaterializeAssessmentHandler(),
            "assurance.quality.reconcile-issues.execute": ReconcileIssuesHandler(),
            "assurance.quality.surface-baseline.execute": SurfaceBaselineHandler(),
        }
    )


__all__ = [
    "MaterializeAssessmentHandler",
    "ReconcileIssuesHandler",
    "SurfaceBaselineHandler",
    "quality_handlers",
]
