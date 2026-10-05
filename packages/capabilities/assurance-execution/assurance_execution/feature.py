"""Deterministic Execution wheel graphs and Product-facing feature bundle."""

from dataclasses import dataclass

from graph_engine.boot import FeatureFactoryRef, FeatureSpec
from graph_engine.flow import BoundFlow

from assurance_execution.contracts.attempts import (
    AGENT_JOB_CONTRACTS,
    OUTPUT_ROUTE_TEMPLATES,
    TASK_ATTEMPT_CONTRACTS,
)
from assurance_execution.plugin import ExecutionPlugin


@dataclass(frozen=True, slots=True)
class ExecutionGraphs:
    execute: BoundFlow
    rerun: BoundFlow


FEATURE = FeatureSpec(
    plugin=ExecutionPlugin,
    agent_contracts=AGENT_JOB_CONTRACTS,
    task_contracts=TASK_ATTEMPT_CONTRACTS,
    output_route_templates=OUTPUT_ROUTE_TEMPLATES,
    graph_factory=FeatureFactoryRef(
        "assurance.execution", "assurance_execution.graphs.factory:build_execution_graphs"
    ),
    bundle_type=ExecutionGraphs,
)

__all__ = ["ExecutionGraphs", "FEATURE"]
