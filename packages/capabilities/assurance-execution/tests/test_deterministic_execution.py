from __future__ import annotations

import pytest
from pydantic import ValidationError

from assurance_execution.contracts.attempts import AGENT_JOB_CONTRACTS, TASK_ATTEMPT_CONTRACTS
from assurance_execution.contracts.evidence import FamilyExecutionOutcomeV1
from assurance_execution.graphs.factory import build_execution_graphs
from graph_engine.testing import GraphHarness


def test_execution_is_a_non_retrying_task_not_an_agent() -> None:
    assert AGENT_JOB_CONTRACTS == {}
    for name in ("execute", "run"):
        contract = TASK_ATTEMPT_CONTRACTS[name]
        assert contract.handler_id == "assurance.execution.run-tests"
        assert contract.retry.max_attempts == 1
        assert contract.timeout.seconds == 3600
        assert contract.contract_id == f"assurance.execution.{name}"
        assert "agent" not in contract.contract_id


def test_retired_execution_agent_contracts_are_not_bound() -> None:
    harness = GraphHarness()
    context = harness.recording_context(
        owner_id="assurance.execution",
        contracts={contract.contract_id: contract for contract in TASK_ATTEMPT_CONTRACTS.values()},
    )
    build_execution_graphs(context)
    assert context.bound_contract_ids == (
        "assurance.execution.execute",
        "assurance.execution.run",
    )
    assert "assurance.execution.agent.execute.v1" not in context.bound_contract_ids
    assert "assurance.execution.agent.run.v1" not in context.bound_contract_ids


def test_blocked_family_requires_reason_and_diagnostics() -> None:
    with pytest.raises(ValidationError):
        FamilyExecutionOutcomeV1.model_validate(
            {"family": "performance", "state": "blocked", "reason_code": "runner_unsupported"}
        )
