from __future__ import annotations

from typing import Literal

from agent_runtime_contracts import AgentExecutionContract

from assurance_healing.contracts.attempts import AGENT_JOB_CONTRACTS, OUTPUT_ROUTE_TEMPLATES

WORKFLOW_MODULE_ID = "assurance.healing.workflow"
WORKFLOW_RESOURCE_ID = "assurance.healing.workflow.module.v1"
WORKFLOW_EXPORTS: tuple[str, ...] = ("repair-failure", "repair-coverage")
REPAIR_ROUND_ADVANCE_ID = "assurance.healing.repair-round.advance"
RepairRoundKind = Literal["failure", "coverage"]
REPAIR_ROUND_KINDS: tuple[RepairRoundKind, ...] = ("coverage", "failure")
HealingRepairOutcome = Literal["exhausted", "failed", "needs_review", "not_eligible", "repaired"]
HEALING_REPAIR_OUTCOMES: tuple[HealingRepairOutcome, ...] = (
    "exhausted",
    "failed",
    "needs_review",
    "not_eligible",
    "repaired",
)
AGENT_SLOT_PHASES: tuple[str, ...] = ("prepare", "execute", "finalize")

__all__ = [
    "AGENT_JOB_CONTRACTS",
    "AGENT_SLOT_PHASES",
    "AgentExecutionContract",
    "OUTPUT_ROUTE_TEMPLATES",
    "HEALING_REPAIR_OUTCOMES",
    "HealingRepairOutcome",
    "REPAIR_ROUND_ADVANCE_ID",
    "REPAIR_ROUND_KINDS",
    "RepairRoundKind",
    "WORKFLOW_EXPORTS",
    "WORKFLOW_MODULE_ID",
    "WORKFLOW_RESOURCE_ID",
]
