"""Intake graphs and their Product-facing feature bundle."""

from __future__ import annotations

from dataclasses import dataclass

from langgraph.graph.state import CompiledStateGraph

from graph_engine.boot import FeatureFactoryRef, FeatureSpec

from assurance_intake.ops import router
from assurance_intake.plugin import IntakePlugin

AGENT_JOB_CONTRACTS = router.agent_contracts()
TASK_ATTEMPT_CONTRACTS = router.task_contracts()
OUTPUT_ROUTE_TEMPLATES = router.output_routes()
attempt_contract_refs = router.attempt_contract_refs


@dataclass(frozen=True, slots=True)
class IntakeGraphs:
    prepare: CompiledStateGraph
    case: CompiledStateGraph


FEATURE = FeatureSpec(
    plugin=IntakePlugin,
    agent_contracts=AGENT_JOB_CONTRACTS,
    task_contracts=TASK_ATTEMPT_CONTRACTS,
    output_route_templates=OUTPUT_ROUTE_TEMPLATES,
    graph_factory=FeatureFactoryRef(
        "assurance.intake", "assurance_intake.graphs.factory:build_intake_graphs"
    ),
)

__all__ = [
    "AGENT_JOB_CONTRACTS",
    "FEATURE",
    "OUTPUT_ROUTE_TEMPLATES",
    "TASK_ATTEMPT_CONTRACTS",
    "IntakeGraphs",
    "attempt_contract_refs",
]
