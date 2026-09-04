from __future__ import annotations

import inspect
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from graph_engine import ENGINE_API_VERSION, RegistryPorts
from graph_engine.plugin_api import AttemptContractRef, TaskContext

from agent_runtime_contracts import AgentExecutionContract
from assurance_healing.contracts.agent import CoverageRepairInputV1, FixProposalInputV1, FixProposalResultV1
from assurance_healing.contracts.attempts import (
    AGENT_JOB_CONTRACTS,
    TASK_ATTEMPT_CONTRACTS,
    attempt_contract_refs,
)
from assurance_healing.contracts.coverage_repair import CoverageRepairStatus
from assurance_healing.contracts.decisions import HealingRepairRoundAdvanceOutput, advance_repair_round
from assurance_healing.operations.workflow_state import HealingRepairRoundAdvanceHandler
from assurance_healing.plugin import HealingPlugin
from tests.product.test_change_local_output_routing import execute_task


def test_healing_owns_two_agent_contracts() -> None:
    assert len(AGENT_JOB_CONTRACTS) == 2
    assert TASK_ATTEMPT_CONTRACTS == {}
    expected = {
        "coverage-repair": (
            "aa-coverage-repair",
            "assurance-v1-test-author",
            CoverageRepairInputV1,
            CoverageRepairStatus,
        ),
        "fix-proposal": (
            "aa-fix-proposal",
            "assurance-v1-doc-author",
            FixProposalInputV1,
            FixProposalResultV1,
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
        assert contract.output_model is result_model
        assert contract.validators == ()
        assert contract.retry.max_attempts == 1
        assert contract.timeout.seconds == 60
        claims = contract.phase_write_claims
        assert set(claims.runtime) == set(contract.resources.writes)


def test_healing_plugin_projects_authenticated_attempt_contracts() -> None:
    refs = attempt_contract_refs()
    contribution = HealingPlugin.contribute(RegistryPorts(engine_api=ENGINE_API_VERSION))
    assert refs == contribution.attempt_contracts == HealingPlugin.descriptor().attempt_contracts
    assert all(isinstance(item, AttemptContractRef) for item in refs)
    assert len(contribution.commit_validators) == 3
    assert all(contract.validators == () for contract in AGENT_JOB_CONTRACTS.values())


def test_repair_round_advance_is_not_a_task_contract() -> None:
    published = {contract.contract_id for contract in AGENT_JOB_CONTRACTS.values()}
    assert "assurance.healing.repair-round.advance" not in published
    assert "context" not in inspect.signature(advance_repair_round).parameters


@pytest.mark.parametrize("kind", ("failure", "coverage"))
async def test_advance_repair_round_matches_legacy_handler_without_touching_context(kind: str) -> None:
    payload = {"kind": kind, "rounds_used": 0, "rounds_budget": 2}
    spy = MagicMock(spec=TaskContext)
    executed = await execute_task(
        HealingRepairRoundAdvanceHandler(),
        payload,
        capability_id="assurance.healing.repair-round.advance",
    )
    output = advance_repair_round(payload)
    assert isinstance(output, HealingRepairRoundAdvanceOutput)
    assert output.model_dump(mode="json") == executed.outcome.output
    assert spy.mock_calls == []


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "failure", "rounds_used": 2, "rounds_budget": 2},
        {"kind": "unknown", "rounds_used": 0, "rounds_budget": 2},
        {"kind": "coverage", "rounds_used": 0, "rounds_budget": 0},
        {"kind": "failure", "rounds_used": 0, "rounds_budget": 2, "extra": True},
    ],
)
def test_advance_repair_round_rejects_invalid_inputs(payload: dict[str, object]) -> None:
    with pytest.raises((ValidationError, ValueError)):
        advance_repair_round(payload)
