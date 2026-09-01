from __future__ import annotations

from agent_runtime_contracts import AgentExecutionContract

from assurance_execution.contracts.attempts import AGENT_JOB_CONTRACTS, OUTPUT_ROUTE_TEMPLATES
from assurance_execution.contracts.execution import EXECUTION_VERDICTS, ExecutionVerdict

WORKFLOW_MODULE_ID = "assurance.execution.workflow"
WORKFLOW_RESOURCE_ID = "assurance.execution.workflow.module.v1"
WORKFLOW_EXPORTS: tuple[str, ...] = ("execute", "rerun")
AGENT_SLOT_PHASES: tuple[str, ...] = ("prepare", "execute", "finalize")

__all__ = [
    "AGENT_JOB_CONTRACTS",
    "AGENT_SLOT_PHASES",
    "AgentExecutionContract",
    "EXECUTION_VERDICTS",
    "ExecutionVerdict",
    "OUTPUT_ROUTE_TEMPLATES",
    "WORKFLOW_EXPORTS",
    "WORKFLOW_MODULE_ID",
    "WORKFLOW_RESOURCE_ID",
]
