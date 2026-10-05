"""Quality graphs and their Product-facing feature bundle."""

from __future__ import annotations

from dataclasses import dataclass

from graph_engine.boot import FeatureFactoryRef, FeatureSpec
from graph_engine.flow import BoundFlow

from assurance_quality.contracts.attempts import (
    AGENT_JOB_CONTRACTS,
    OUTPUT_ROUTE_TEMPLATES,
    TASK_ATTEMPT_CONTRACTS,
)
from assurance_quality.plugin import QualityPlugin


@dataclass(frozen=True, slots=True)
class QualityGraphs:
    assess: BoundFlow
    issue_review: BoundFlow
    issue_analyze: BoundFlow
    issue_reconcile: BoundFlow
    report: BoundFlow
    fact_baseline: BoundFlow
    surface_baseline: BoundFlow


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
