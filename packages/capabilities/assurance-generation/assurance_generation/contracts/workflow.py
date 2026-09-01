from __future__ import annotations

from agent_runtime_contracts import AgentExecutionContract

from assurance_generation.contracts.attempts import AGENT_JOB_CONTRACTS, OUTPUT_ROUTE_TEMPLATES
from assurance_generation.contracts.families import GENERATION_FAMILIES, validate_selected_families

WORKFLOW_MODULE_ID = "assurance.generation.workflow"
WORKFLOW_RESOURCE_ID = "assurance.generation.workflow.module.v1"
WORKFLOW_EXPORTS: tuple[str, ...] = ("generate",)
AGENT_SLOT_PHASES: tuple[str, ...] = ("prepare", "execute", "finalize")

__all__ = [
    "AGENT_JOB_CONTRACTS",
    "AGENT_SLOT_PHASES",
    "AgentExecutionContract",
    "GENERATION_FAMILIES",
    "OUTPUT_ROUTE_TEMPLATES",
    "WORKFLOW_EXPORTS",
    "WORKFLOW_MODULE_ID",
    "WORKFLOW_RESOURCE_ID",
    "validate_selected_families",
]
