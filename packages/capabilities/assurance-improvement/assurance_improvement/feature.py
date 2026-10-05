"""Improvement graphs and their Product-facing feature bundle."""

from __future__ import annotations

from dataclasses import dataclass

from graph_engine.boot import FeatureFactoryRef, FeatureSpec
from graph_engine.flow import BoundFlow

from assurance_improvement.contracts.attempts import (
    AGENT_JOB_CONTRACTS,
    OUTPUT_ROUTE_TEMPLATES,
    TASK_ATTEMPT_CONTRACTS,
)
from assurance_improvement.plugin import ImprovementPlugin


@dataclass(frozen=True, slots=True)
class ImprovementGraphs:
    archive: BoundFlow
    retro: BoundFlow
    review: BoundFlow
    evaluate: BoundFlow
    export: BoundFlow
    apply: BoundFlow
    rollback: BoundFlow
    runtime_snapshot: BoundFlow


FEATURE = FeatureSpec(
    plugin=ImprovementPlugin,
    agent_contracts=AGENT_JOB_CONTRACTS,
    task_contracts=TASK_ATTEMPT_CONTRACTS,
    output_route_templates=OUTPUT_ROUTE_TEMPLATES,
    graph_factory=FeatureFactoryRef(
        "assurance.improvement",
        "assurance_improvement.graphs.factory:build_improvement_graphs",
    ),
    bundle_type=ImprovementGraphs,
)

__all__ = ["FEATURE", "ImprovementGraphs"]
