from __future__ import annotations

from types import MappingProxyType
from typing import Mapping

from agent_runtime_contracts import AgentExecutionContract
from graph_engine.attempts import TaskAttemptContract

from assurance_execution.contracts.attempts import AGENT_JOB_CONTRACTS as EXECUTION_AGENT_JOB_CONTRACTS
from assurance_generation.contracts.attempts import AGENT_JOB_CONTRACTS as GENERATION_AGENT_JOB_CONTRACTS
from assurance_healing.contracts.attempts import AGENT_JOB_CONTRACTS as HEALING_AGENT_JOB_CONTRACTS
from assurance_improvement.contracts.attempts import (
    AGENT_JOB_CONTRACTS as IMPROVEMENT_AGENT_JOB_CONTRACTS,
)
from assurance_improvement.contracts.attempts import (
    TASK_ATTEMPT_CONTRACTS as IMPROVEMENT_TASK_ATTEMPT_CONTRACTS,
)
from assurance_intake.contracts.attempts import AGENT_JOB_CONTRACTS as INTAKE_AGENT_JOB_CONTRACTS
from assurance_quality.contracts.attempts import AGENT_JOB_CONTRACTS as QUALITY_AGENT_JOB_CONTRACTS

FEATURE_AGENT_JOB_CATALOGS: tuple[Mapping[str, AgentExecutionContract], ...] = (
    INTAKE_AGENT_JOB_CONTRACTS,
    GENERATION_AGENT_JOB_CONTRACTS,
    EXECUTION_AGENT_JOB_CONTRACTS,
    QUALITY_AGENT_JOB_CONTRACTS,
    HEALING_AGENT_JOB_CONTRACTS,
    IMPROVEMENT_AGENT_JOB_CONTRACTS,
)

AGENT_EXECUTION_CONTRACTS: Mapping[str, AgentExecutionContract] = MappingProxyType(
    {
        contract.contract_id: contract
        for catalog in FEATURE_AGENT_JOB_CATALOGS
        for contract in catalog.values()
    }
)
FEATURE_TASK_ATTEMPT_CONTRACTS: Mapping[str, TaskAttemptContract] = IMPROVEMENT_TASK_ATTEMPT_CONTRACTS


def all_feature_agent_contracts() -> Mapping[str, AgentExecutionContract]:
    return AGENT_EXECUTION_CONTRACTS


def all_feature_task_contracts() -> Mapping[str, TaskAttemptContract]:
    return FEATURE_TASK_ATTEMPT_CONTRACTS


def is_agent_contract(contract: TaskAttemptContract) -> bool:
    return ".agent." in contract.contract_id


__all__ = [
    "AGENT_EXECUTION_CONTRACTS",
    "FEATURE_AGENT_JOB_CATALOGS",
    "FEATURE_TASK_ATTEMPT_CONTRACTS",
    "AgentExecutionContract",
    "all_feature_agent_contracts",
    "all_feature_task_contracts",
    "is_agent_contract",
]
