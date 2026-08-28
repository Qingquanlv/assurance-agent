from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from agent_runtime_contracts import AgentExecutionContract
from graph_engine.plugin_api import ResourceClaimTemplate

WORKFLOW_MODULE_ID = "assurance.execution.workflow"
WORKFLOW_RESOURCE_ID = "assurance.execution.workflow.module.v1"
WORKFLOW_EXPORTS: tuple[str, ...] = ("execute", "rerun")
AGENT_SLOT_PHASES: tuple[str, ...] = ("prepare", "execute", "finalize")

_EXECUTOR = "assurance-v1-executor"


def _paths(*suffixes: str) -> tuple[str, ...]:
    return tuple(sorted(f"qa/changes/{{change_id}}/{suffix}" for suffix in suffixes))


def _job(base: str, skill_id: str, outputs: tuple[str, ...]) -> AgentExecutionContract:
    return AgentExecutionContract(
        contract_id=f"assurance.execution.agent.{base}.v1",
        skill_id=skill_id,
        agent_profile=_EXECUTOR,
        resources=ResourceClaimTemplate(
            parameters={"change_id": "/workspace/scope_id"},
            reads=("qa",),
            writes=_paths(*outputs),
        ),
    )


_JOBS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("execute", "aa-execute", ("execution/execute-result.json",)),
    ("run", "aa-run", ("execution/run-result.json",)),
)

AGENT_JOB_CONTRACTS: Mapping[str, AgentExecutionContract] = MappingProxyType(
    {base: _job(base, skill_id, outputs) for base, skill_id, outputs in _JOBS}
)
OUTPUT_ROUTE_TEMPLATES: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {base: _paths(*outputs) for base, _skill_id, outputs in _JOBS}
)

__all__ = [
    "AGENT_JOB_CONTRACTS",
    "AGENT_SLOT_PHASES",
    "OUTPUT_ROUTE_TEMPLATES",
    "WORKFLOW_EXPORTS",
    "WORKFLOW_MODULE_ID",
    "WORKFLOW_RESOURCE_ID",
]
