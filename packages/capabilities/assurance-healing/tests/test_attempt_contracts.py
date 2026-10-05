from __future__ import annotations


from graph_engine import ENGINE_API_VERSION, RegistryPorts
from graph_engine.plugin_api import AttemptContractRef

from agent_runtime_contracts import AgentExecutionContract
from assurance_healing.contracts.agent import FixProposalResultV1
from assurance_healing.contracts.application import (
    TestRepairResultV1 as RepairAgentResultV1,
    VerifiedTestRepairV1,
)
from assurance_healing.contracts.repair_input import ApplyBoundInputV1, RepairBoundInputV1
from assurance_healing.contracts.attempts import (
    AGENT_JOB_CONTRACTS,
    TASK_ATTEMPT_CONTRACTS,
    attempt_contract_refs,
)
from assurance_healing.plugin import HealingPlugin


def test_healing_owns_two_agent_contracts() -> None:
    assert len(AGENT_JOB_CONTRACTS) == 2
    assert TASK_ATTEMPT_CONTRACTS == {}
    expected = {
        "fix-proposal": (
            "aa-fix-proposal",
            "assurance-v1-doc-author",
            RepairBoundInputV1,
            FixProposalResultV1,
        ),
        "apply-test-repair": (
            "aa-apply-test-repair",
            "assurance-v1-test-author",
            ApplyBoundInputV1,
            RepairAgentResultV1,
        ),
    }
    assert set(AGENT_JOB_CONTRACTS) == set(expected)
    for base, (skill_id, profile, input_model, result_model) in expected.items():
        contract = AGENT_JOB_CONTRACTS[base]
        assert isinstance(contract, AgentExecutionContract)
        assert contract.contract_id == f"assurance.healing.agent.{base}.v1"
        assert contract.owner_id == "assurance.healing"
        assert contract.prepare_handler_id == f"assurance.healing.{base}.prepare"
        assert contract.finalize_handler_id == f"assurance.healing.{base}.finalize"
        assert contract.skill_id == skill_id
        assert contract.agent_profile == profile
        assert contract.input_model is input_model
        assert contract.agent_result_model is result_model
        expected_output_model = VerifiedTestRepairV1 if base == "apply-test-repair" else result_model
        assert contract.output_model is expected_output_model
        assert contract.validators == ()
        assert contract.retry.max_attempts == 10
        assert contract.retry.interval_seconds == 10
        assert contract.timeout.seconds == 60
        claims = contract.phase_write_claims
        assert set(claims.runtime) | set(claims.finalize) == set(contract.resources.writes)
        assert not (set(claims.runtime) & set(claims.finalize))


def test_healing_plugin_projects_authenticated_attempt_contracts() -> None:
    refs = attempt_contract_refs()
    contribution = HealingPlugin.contribute(RegistryPorts(engine_api=ENGINE_API_VERSION))
    assert refs == contribution.attempt_contracts == HealingPlugin.descriptor().attempt_contracts
    assert all(isinstance(item, AttemptContractRef) for item in refs)
    assert contribution.commit_validators == {}
    assert all(contract.validators == () for contract in AGENT_JOB_CONTRACTS.values())
