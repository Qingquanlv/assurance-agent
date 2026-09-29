"""Static composition interface for the Healing wheel."""

from graph_engine.boot import FeatureFactoryRef, FeatureSpec

from assurance_healing.contracts.attempts import (
    AGENT_JOB_CONTRACTS,
    OUTPUT_ROUTE_TEMPLATES,
    TASK_ATTEMPT_CONTRACTS,
)
from assurance_healing.operations.agent_tasks import (
    ApplyTestRepairTask,
    CoverageRepairTask,
    FixProposalTask,
)
from assurance_healing.plugin import HealingPlugin

FEATURE = FeatureSpec(
    plugin=HealingPlugin,
    agent_contracts=AGENT_JOB_CONTRACTS,
    task_contracts=TASK_ATTEMPT_CONTRACTS,
    output_route_templates=OUTPUT_ROUTE_TEMPLATES,
    graph_factory=FeatureFactoryRef(
        "assurance.healing", "assurance_healing.graphs.factory:build_healing_graphs"
    ),
    agent_task_types=(FixProposalTask, ApplyTestRepairTask, CoverageRepairTask),
)

__all__ = ["FEATURE"]
