from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, Literal, cast

from agent_runtime_contracts.qa_paths import qa_join
from graph_engine.attempts import AttemptRetryPolicy, AttemptTimeoutPolicy, TaskAttemptContract
from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.plugin_api import AttemptContractRef, ResourceClaimTemplate

from assurance_execution.contracts.agent import ExecutionPrepareInputV1, RerunPrepareInputV1
from graph_engine.stategraph.ledger import NamedWrite

from assurance_execution.contracts.workflow import (
    EXECUTION_CYCLE_PATH,
    ExecutionAttemptOutputV1,
    execution_evidence_path,
    execution_node_for_kind,
)

_TASK_RETRY = AttemptRetryPolicy(max_attempts=1)
_TASK_TIMEOUT = AttemptTimeoutPolicy(seconds=3600)
AGENT_JOB_CONTRACTS: Mapping[str, Any] = MappingProxyType({})


def _task(base: Literal["execute", "run"]) -> TaskAttemptContract[Any, Any]:
    return TaskAttemptContract(
        contract_id=f"assurance.execution.{base}",
        owner_id="assurance.execution",
        handler_id="assurance.execution.run-tests",
        input_model=RerunPrepareInputV1 if base == "run" else ExecutionPrepareInputV1,
        output_model=ExecutionAttemptOutputV1,
        resources=ResourceClaimTemplate(
            parameters={"coverage_epoch": "/coverage_epoch_token"},
            reads=("qa",),
            writes=(
                execution_evidence_path(execution_node_for_kind(base)),
                EXECUTION_CYCLE_PATH,
                qa_join("execution/epochs/{coverage_epoch}"),
                qa_join(".staging/execution/durable-execution-v1.json"),
            ),
        ),
        retry=_TASK_RETRY,
        timeout=_TASK_TIMEOUT,
        validators=(),
        writes=(NamedWrite("cycle", EXECUTION_CYCLE_PATH),),
    )


OUTPUT_ROUTE_TEMPLATES: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {base: (execution_evidence_path(execution_node_for_kind(base)),) for base in ("execute", "run")}
)
TASK_ATTEMPT_CONTRACTS: Mapping[str, TaskAttemptContract[Any, Any]] = MappingProxyType(
    {base: _task(base) for base in ("execute", "run")}
)


def attempt_contract_refs() -> tuple[AttemptContractRef, ...]:
    return tuple(
        sorted(
            (
                AttemptContractRef(
                    contract_id=contract.contract_id,
                    digest=canonical_digest(cast(JSONValue, contract.canonical_projection())),
                )
                for contract in TASK_ATTEMPT_CONTRACTS.values()
            ),
            key=lambda item: item.contract_id,
        )
    )


__all__ = [
    "AGENT_JOB_CONTRACTS",
    "OUTPUT_ROUTE_TEMPLATES",
    "TASK_ATTEMPT_CONTRACTS",
    "attempt_contract_refs",
]
