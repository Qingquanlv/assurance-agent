from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Literal

from agent_runtime_contracts import AgentExecutionContract
from graph_engine.plugin_api import ResourceClaimTemplate

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

_DOC_AUTHOR = "assurance-v1-doc-author"
_TEST_AUTHOR = "assurance-v1-test-author"


def _paths(*suffixes: str) -> tuple[str, ...]:
    return tuple(sorted(f"qa/changes/{{change_id}}/{suffix}" for suffix in suffixes))


def _job(base: str, skill_id: str, agent_profile: str, outputs: tuple[str, ...]) -> AgentExecutionContract:
    return AgentExecutionContract(
        contract_id=f"assurance.healing.agent.{base}.v1",
        skill_id=skill_id,
        agent_profile=agent_profile,
        resources=ResourceClaimTemplate(
            parameters={"change_id": "/workspace/scope_id"},
            reads=("qa",),
            writes=_paths(*outputs),
        ),
    )


_JOBS: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    ("coverage-repair", "aa-coverage-repair", _TEST_AUTHOR, ("healing/coverage-repair.json",)),
    ("fix-proposal", "aa-fix-proposal", _DOC_AUTHOR, ("healing/fix-proposal.json",)),
)

AGENT_JOB_CONTRACTS: Mapping[str, AgentExecutionContract] = MappingProxyType(
    {base: _job(base, skill_id, agent_profile, outputs) for base, skill_id, agent_profile, outputs in _JOBS}
)
OUTPUT_ROUTE_TEMPLATES: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {base: _paths(*outputs) for base, _skill_id, _profile, outputs in _JOBS}
)

__all__ = [
    "AGENT_JOB_CONTRACTS",
    "AGENT_SLOT_PHASES",
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
