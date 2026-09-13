from __future__ import annotations

from graph_engine import ENGINE_API_VERSION, RegistryPorts
from graph_engine.plugin_api import AttemptContractRef

from agent_runtime_contracts import AgentExecutionContract
from assurance_execution.contracts.agent import ExecutionPrepareInputV1
from assurance_execution.contracts.attempts import (
    AGENT_JOB_CONTRACTS,
    TASK_ATTEMPT_CONTRACTS,
    attempt_contract_refs,
)
from assurance_execution.contracts.evidence import ExecutionAgentResultV1, ExecutionEvidenceV1
from assurance_execution.plugin import ExecutionPlugin


def test_execution_owns_two_agent_contracts() -> None:
    assert len(AGENT_JOB_CONTRACTS) == 2
    assert TASK_ATTEMPT_CONTRACTS == {}
    expected = {
        "execute": ("aa-execute", ExecutionPrepareInputV1),
        "run": ("aa-run", ExecutionPrepareInputV1),
    }
    assert set(AGENT_JOB_CONTRACTS) == set(expected)
    for base, (skill_id, input_model) in expected.items():
        contract = AGENT_JOB_CONTRACTS[base]
        assert isinstance(contract, AgentExecutionContract)
        assert contract.contract_id == f"assurance.execution.agent.{base}.v1"
        assert contract.owner_id == "assurance.execution"
        assert contract.prepare_handler_id == f"assurance.execution.{base}.prepare"
        assert contract.finalize_handler_id == f"assurance.execution.{base}.finalize"
        assert contract.skill_id == skill_id
        assert contract.agent_profile == "assurance-v1-executor"
        assert contract.input_model is input_model
        assert contract.agent_result_model is ExecutionAgentResultV1
        assert contract.output_model is ExecutionEvidenceV1
        assert contract.validators == ()
        assert contract.retry.max_attempts == 10
        assert contract.retry.interval_seconds == 10
        assert contract.timeout.seconds == 60
        claims = contract.phase_write_claims
        assert claims.prepare == ("qa/.staging/execution",)
        assert claims.runtime == ()
        assert claims.finalize == (
            f"qa/results/execution/{'run' if base == 'run' else 'execute'}-result.json",
        )
        assert set((*claims.prepare, *claims.finalize)) == set(contract.resources.writes)


def test_execution_plugin_projects_authenticated_attempt_contracts() -> None:
    refs = attempt_contract_refs()
    contribution = ExecutionPlugin.contribute(RegistryPorts(engine_api=ENGINE_API_VERSION))
    assert refs == contribution.attempt_contracts == ExecutionPlugin.descriptor().attempt_contracts
    assert all(isinstance(item, AttemptContractRef) for item in refs)
    assert len(contribution.commit_validators) == 2
    assert all(contract.validators == () for contract in AGENT_JOB_CONTRACTS.values())
