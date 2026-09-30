"""Deterministic Execution wheel graphs and Product-facing feature bundle."""

from dataclasses import dataclass

from langgraph.graph.state import CompiledStateGraph
from graph_engine.boot import FeatureFactoryRef, FeatureSpec

from assurance_execution.contracts.attempts import (
    AGENT_JOB_CONTRACTS,
    OUTPUT_ROUTE_TEMPLATES,
    TASK_ATTEMPT_CONTRACTS,
)
from assurance_execution.plugin import ExecutionPlugin


@dataclass(frozen=True, slots=True)
class ExecutionGraphs:
    execute: CompiledStateGraph
    rerun: CompiledStateGraph


FEATURE = FeatureSpec(
    plugin=ExecutionPlugin,
    agent_contracts=AGENT_JOB_CONTRACTS,
    task_contracts=TASK_ATTEMPT_CONTRACTS,
    output_route_templates=OUTPUT_ROUTE_TEMPLATES,
    graph_factory=FeatureFactoryRef(
        "assurance.execution", "assurance_execution.graphs.factory:build_execution_graphs"
    ),
)

__all__ = ["ExecutionGraphs", "FEATURE"]
