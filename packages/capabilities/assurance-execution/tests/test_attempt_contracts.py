from __future__ import annotations

from graph_engine import ENGINE_API_VERSION, RegistryPorts
from graph_engine.attempts import TaskAttemptContract
from graph_engine.plugin_api import AttemptContractRef

from assurance_execution.contracts.agent import ExecutionPrepareInputV1, RerunPrepareInputV1
from assurance_execution.contracts.attempts import (
    AGENT_JOB_CONTRACTS,
    TASK_ATTEMPT_CONTRACTS,
    attempt_contract_refs,
)
from assurance_execution.contracts.workflow import ExecutionAttemptOutputV1
from assurance_execution.plugin import ExecutionPlugin


def test_execution_owns_two_non_retrying_task_contracts() -> None:
    assert AGENT_JOB_CONTRACTS == {}
    assert set(TASK_ATTEMPT_CONTRACTS) == {"execute", "run"}
    for base, contract in TASK_ATTEMPT_CONTRACTS.items():
        assert isinstance(contract, TaskAttemptContract)
        assert contract.contract_id == f"assurance.execution.{base}"
        assert contract.owner_id == "assurance.execution"
        assert contract.handler_id == "assurance.execution.run-tests"
        assert contract.input_model is (RerunPrepareInputV1 if base == "run" else ExecutionPrepareInputV1)
        assert contract.output_model is ExecutionAttemptOutputV1
        assert contract.validators == ()
        assert contract.retry.max_attempts == 1
        assert contract.timeout.seconds == 3600


def test_execution_plugin_projects_authenticated_attempt_contracts() -> None:
    refs = attempt_contract_refs()
    contribution = ExecutionPlugin.contribute(RegistryPorts(engine_api=ENGINE_API_VERSION))
    assert refs == contribution.attempt_contracts == ExecutionPlugin.descriptor().attempt_contracts
    assert all(isinstance(item, AttemptContractRef) for item in refs)
    assert {item.contract_id for item in refs} == {
        "assurance.execution.execute",
        "assurance.execution.run",
    }
    assert "assurance.execution.execute.prepare" not in contribution.task_handlers
    assert "assurance.execution.run-tests-and-collect-pr-metrics" not in contribution.task_handlers
    assert "assurance.execution.run-tests" in contribution.task_handlers
    assert all(contract.validators == () for contract in TASK_ATTEMPT_CONTRACTS.values())
