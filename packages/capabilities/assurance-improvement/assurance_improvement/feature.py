"""Improvement graphs and their Product-facing feature bundle."""

from __future__ import annotations

from dataclasses import dataclass

from langgraph.graph.state import CompiledStateGraph
from graph_engine.boot import FeatureFactoryRef, FeatureSpec

from assurance_improvement.contracts.attempts import (
    AGENT_JOB_CONTRACTS,
    OUTPUT_ROUTE_TEMPLATES,
    TASK_ATTEMPT_CONTRACTS,
)
from assurance_improvement.plugin import ImprovementPlugin


@dataclass(frozen=True, slots=True)
class ImprovementGraphs:
    archive: CompiledStateGraph
    retro: CompiledStateGraph
    review: CompiledStateGraph
    evaluate: CompiledStateGraph
    export: CompiledStateGraph
    apply: CompiledStateGraph
    rollback: CompiledStateGraph


FEATURE = FeatureSpec(
    plugin=ImprovementPlugin,
    agent_contracts=AGENT_JOB_CONTRACTS,
    task_contracts=TASK_ATTEMPT_CONTRACTS,
    output_route_templates=OUTPUT_ROUTE_TEMPLATES,
    graph_factory=FeatureFactoryRef(
        "assurance.improvement",
        "assurance_improvement.graphs.factory:build_improvement_graphs",
    ),
)

__all__ = ["FEATURE", "ImprovementGraphs"]
