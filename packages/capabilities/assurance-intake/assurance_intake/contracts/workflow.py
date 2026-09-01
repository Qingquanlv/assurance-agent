from __future__ import annotations

from agent_runtime_contracts import AgentExecutionContract

from assurance_intake.contracts.attempts import AGENT_JOB_CONTRACTS, OUTPUT_ROUTE_TEMPLATES

WORKFLOW_MODULE_ID = "assurance.intake.workflow"
WORKFLOW_RESOURCE_ID = "assurance.intake.workflow.module.v1"
WORKFLOW_EXPORTS: tuple[str, ...] = ("prepare", "case")
AGENT_SLOT_PHASES: tuple[str, ...] = ("prepare", "execute", "finalize")

__all__ = [
    "AGENT_JOB_CONTRACTS",
    "AGENT_SLOT_PHASES",
    "AgentExecutionContract",
    "OUTPUT_ROUTE_TEMPLATES",
    "WORKFLOW_EXPORTS",
    "WORKFLOW_MODULE_ID",
    "WORKFLOW_RESOURCE_ID",
]
