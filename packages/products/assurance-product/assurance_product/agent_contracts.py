from __future__ import annotations

from types import MappingProxyType
from typing import Mapping

from agent_runtime_contracts import AgentExecutionContract
from graph_engine.attempts import TaskAttemptContract

from assurance_product.features import FEATURES

FEATURE_AGENT_JOB_CATALOGS: tuple[Mapping[str, AgentExecutionContract], ...] = (
    *(feature.agent_contracts for feature in FEATURES if feature.agent_contracts),
)

AGENT_EXECUTION_CONTRACTS: Mapping[str, AgentExecutionContract] = MappingProxyType(
    {
        contract.contract_id: contract
        for catalog in FEATURE_AGENT_JOB_CATALOGS
        for contract in catalog.values()
    }
)
FEATURE_TASK_ATTEMPT_CONTRACTS: Mapping[str, TaskAttemptContract] = MappingProxyType(
    {name: contract for feature in FEATURES for name, contract in feature.task_contracts.items()}
)


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
