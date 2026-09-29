"""Static composition interface for the Intake wheel."""

from dataclasses import dataclass

from langgraph.graph.state import CompiledStateGraph
from graph_engine.boot import FeatureFactoryRef, FeatureSpec

from assurance_intake.contracts.attempts import (
    AGENT_JOB_CONTRACTS,
    OUTPUT_ROUTE_TEMPLATES,
    TASK_ATTEMPT_CONTRACTS,
)
from assurance_intake.operations.agent_tasks import CaseReviewTask, ExploreTask, IntakeTask
from assurance_intake.operations.case_design import CaseDesignTask
from assurance_intake.plugin import IntakePlugin


@dataclass(frozen=True, slots=True)
class IntakeGraphs:
    prepare: CompiledStateGraph
    load_plan: CompiledStateGraph
    case: CompiledStateGraph


FEATURE = FeatureSpec(
    plugin=IntakePlugin,
    agent_contracts=AGENT_JOB_CONTRACTS,
    task_contracts=TASK_ATTEMPT_CONTRACTS,
    output_route_templates=OUTPUT_ROUTE_TEMPLATES,
    graph_factory=FeatureFactoryRef(
        "assurance.intake", "assurance_intake.graphs.factory:build_intake_graphs"
    ),
    agent_task_types=(IntakeTask, ExploreTask, CaseDesignTask, CaseReviewTask),
)

__all__ = ["FEATURE", "IntakeGraphs"]
