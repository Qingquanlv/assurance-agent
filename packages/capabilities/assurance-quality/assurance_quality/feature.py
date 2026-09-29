"""Static composition interface for the Quality wheel."""

from graph_engine.boot import FeatureFactoryRef, FeatureSpec

from assurance_quality.contracts.attempts import (
    AGENT_JOB_CONTRACTS,
    OUTPUT_ROUTE_TEMPLATES,
    TASK_ATTEMPT_CONTRACTS,
)
from assurance_quality.operations.agent_tasks import (
    FactBaselineTask,
    InspectTask,
    IssueAnalysisTask,
    IssueTriageTask,
    ReportTask,
)
from assurance_quality.plugin import QualityPlugin

FEATURE = FeatureSpec(
    plugin=QualityPlugin,
    agent_contracts=AGENT_JOB_CONTRACTS,
    task_contracts=TASK_ATTEMPT_CONTRACTS,
    output_route_templates=OUTPUT_ROUTE_TEMPLATES,
    graph_factory=FeatureFactoryRef(
        "assurance.quality", "assurance_quality.graphs.factory:build_quality_graphs"
    ),
    agent_task_types=(FactBaselineTask, InspectTask, IssueTriageTask, IssueAnalysisTask, ReportTask),
)

__all__ = ["FEATURE"]
