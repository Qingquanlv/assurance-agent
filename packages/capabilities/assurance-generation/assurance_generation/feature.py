"""Static composition interface for the Generation wheel."""

from dataclasses import dataclass

from langgraph.graph.state import CompiledStateGraph
from graph_engine.boot import FeatureFactoryRef, FeatureSpec

from assurance_generation.contracts.attempts import (
    AGENT_JOB_CONTRACTS,
    OUTPUT_ROUTE_TEMPLATES,
    TASK_ATTEMPT_CONTRACTS,
)
from assurance_generation.operations.agent_tasks import (
    ApiCodegenReviewTask,
    ApiCodegenTask,
    E2ECodegenReviewTask,
    E2ECodegenTask,
    FuzzCodegenReviewTask,
    FuzzCodegenTask,
    PerformanceCodegenReviewTask,
    PerformanceCodegenTask,
)
from assurance_generation.plugin import GenerationPlugin


@dataclass(frozen=True, slots=True)
class GenerationGraphs:
    generation: CompiledStateGraph
    api: CompiledStateGraph
    e2e: CompiledStateGraph
    fuzz: CompiledStateGraph
    performance: CompiledStateGraph
    init_runtime: CompiledStateGraph
    resolve_inputs: CompiledStateGraph


FEATURE = FeatureSpec(
    plugin=GenerationPlugin,
    agent_contracts=AGENT_JOB_CONTRACTS,
    task_contracts=TASK_ATTEMPT_CONTRACTS,
    output_route_templates=OUTPUT_ROUTE_TEMPLATES,
    graph_factory=FeatureFactoryRef(
        "assurance.generation", "assurance_generation.graphs.factory:build_generation_graphs"
    ),
    agent_task_types=(
        ApiCodegenTask,
        ApiCodegenReviewTask,
        E2ECodegenTask,
        E2ECodegenReviewTask,
        FuzzCodegenTask,
        FuzzCodegenReviewTask,
        PerformanceCodegenTask,
        PerformanceCodegenReviewTask,
    ),
)

__all__ = ["FEATURE", "GenerationGraphs"]
