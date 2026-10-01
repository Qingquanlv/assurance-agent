"""Quality graphs and their Product-facing feature bundle."""

from __future__ import annotations

from dataclasses import dataclass

from langgraph.graph.state import CompiledStateGraph
from graph_engine.boot import FeatureFactoryRef, FeatureSpec

from assurance_quality.contracts.attempts import (
    AGENT_JOB_CONTRACTS,
    OUTPUT_ROUTE_TEMPLATES,
    TASK_ATTEMPT_CONTRACTS,
)
from assurance_quality.plugin import QualityPlugin


@dataclass(frozen=True, slots=True)
class QualityGraphs:
    assess: CompiledStateGraph
    issue_review: CompiledStateGraph
    issue_analyze: CompiledStateGraph
    issue_reconcile: CompiledStateGraph
    report: CompiledStateGraph
    fact_baseline: CompiledStateGraph
    surface_baseline: CompiledStateGraph


FEATURE = FeatureSpec(
    plugin=QualityPlugin,
    agent_contracts=AGENT_JOB_CONTRACTS,
    task_contracts=TASK_ATTEMPT_CONTRACTS,
    output_route_templates=OUTPUT_ROUTE_TEMPLATES,
    graph_factory=FeatureFactoryRef(
        "assurance.quality", "assurance_quality.graphs.factory:build_quality_graphs"
    ),
    bundle_type=QualityGraphs,
)

__all__ = ["FEATURE", "QualityGraphs"]
