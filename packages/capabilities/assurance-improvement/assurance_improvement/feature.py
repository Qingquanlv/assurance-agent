"""Static composition interface for the Improvement wheel."""

from graph_engine.boot import FeatureFactoryRef, FeatureSpec

from assurance_improvement.contracts.attempts import (
    AGENT_JOB_CONTRACTS,
    OUTPUT_ROUTE_TEMPLATES,
    TASK_ATTEMPT_CONTRACTS,
)
from assurance_improvement.operations.agent_tasks import (
    ArchiveTask,
    ImprovementReviewTask,
    RetroEvalAnalysisTask,
    RetroIssueAnalysisTask,
    RetroTask,
    RetroWorkflowAnalysisTask,
)
from assurance_improvement.plugin import ImprovementPlugin

FEATURE = FeatureSpec(
    plugin=ImprovementPlugin,
    agent_contracts=AGENT_JOB_CONTRACTS,
    task_contracts=TASK_ATTEMPT_CONTRACTS,
    output_route_templates=OUTPUT_ROUTE_TEMPLATES,
    graph_factory=FeatureFactoryRef(
        "assurance.improvement", "assurance_improvement.graphs.factory:build_improvement_graphs"
    ),
    agent_task_types=(
        ArchiveTask,
        ImprovementReviewTask,
        RetroEvalAnalysisTask,
        RetroIssueAnalysisTask,
        RetroWorkflowAnalysisTask,
        RetroTask,
    ),
)

__all__ = ["FEATURE"]
