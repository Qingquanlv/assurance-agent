from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agent_runtime_contracts import AgentExecutionContract

if TYPE_CHECKING:
    from assurance_improvement.contracts.attempts import AGENT_JOB_CONTRACTS, OUTPUT_ROUTE_TEMPLATES

WORKFLOW_MODULE_ID = "assurance.improvement.workflow"
WORKFLOW_RESOURCE_ID = "assurance.improvement.workflow.module.v1"
WORKFLOW_EXPORTS: tuple[str, ...] = (
    "archive",
    "retro",
    "review",
    "evaluate",
    "export",
    "apply",
    "rollback",
)
APPLY_HUMAN_ACTIONS: tuple[str, ...] = ("approve", "reject", "request_rework", "supersede")
AUTO_REVIEW_DECISIONS: tuple[str, ...] = ("pass", "changes_requested", "needs_human_review", "reject")
APPLY_EVALUATION_OUTCOMES: tuple[str, ...] = ("passed", "regressed", "awaiting_baseline", "error")
AGENT_SLOT_PHASES: tuple[str, ...] = ("prepare", "execute", "finalize")


def __getattr__(name: str) -> Any:
    if name in {"AGENT_JOB_CONTRACTS", "OUTPUT_ROUTE_TEMPLATES"}:
        from assurance_improvement.contracts.attempts import AGENT_JOB_CONTRACTS, OUTPUT_ROUTE_TEMPLATES

        globals()["AGENT_JOB_CONTRACTS"] = AGENT_JOB_CONTRACTS
        globals()["OUTPUT_ROUTE_TEMPLATES"] = OUTPUT_ROUTE_TEMPLATES
        return globals()[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "AGENT_JOB_CONTRACTS",
    "AGENT_SLOT_PHASES",
    "AgentExecutionContract",
    "OUTPUT_ROUTE_TEMPLATES",
    "APPLY_EVALUATION_OUTCOMES",
    "APPLY_HUMAN_ACTIONS",
    "AUTO_REVIEW_DECISIONS",
    "WORKFLOW_EXPORTS",
    "WORKFLOW_MODULE_ID",
    "WORKFLOW_RESOURCE_ID",
]
