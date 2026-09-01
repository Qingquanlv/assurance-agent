from __future__ import annotations

from pathlib import Path

from tests.product.validator_parity_fixture import (
    ACCEPT_PATH,
    EVIDENCE_VALIDATOR_ID,
    REJECT_PATH,
    TEST_CONTRACT_ID,
    assert_production_inventory_unbound,
    run_validator_parity_candidate,
)


def test_accepted_candidate_validates_once_and_promotes_on_both_runtimes(tmp_path: Path) -> None:
    result = run_validator_parity_candidate(tmp_path, relative=ACCEPT_PATH)
    assert result.legacy.validator_calls == 1
    assert result.langgraph.validator_calls == 1
    assert result.legacy.promoted is True
    assert result.langgraph.promoted is True
    assert result.legacy.prepare_calls >= 1
    assert result.langgraph.prepare_calls >= 1
    assert result.legacy.rejection is None
    assert result.langgraph.rejection is None
    assert_production_inventory_unbound()


def test_rejected_candidate_validates_once_and_never_prepares_or_promotes(tmp_path: Path) -> None:
    result = run_validator_parity_candidate(tmp_path, relative=REJECT_PATH)
    assert result.legacy.validator_calls == 1
    assert result.langgraph.validator_calls == 1
    assert result.legacy.rejection is not None
    assert result.langgraph.rejection is not None
    assert result.legacy.rejection.reason == result.langgraph.rejection.reason
    assert result.legacy.prepare_calls == 0
    assert result.langgraph.prepare_calls == 0
    assert result.legacy.promoted is False
    assert result.langgraph.promoted is False
    assert result.legacy.durable_commit_prepare is False
    assert result.langgraph.durable_commit_prepare is False
    assert_production_inventory_unbound()


def test_test_only_clones_never_enter_production_inventory() -> None:
    from assurance_product.agent_contracts import all_feature_agent_contracts
    from assurance_product.runtime_bindings import AGENT_RUNTIME_BINDINGS

    assert TEST_CONTRACT_ID not in all_feature_agent_contracts()
    assert TEST_CONTRACT_ID not in AGENT_RUNTIME_BINDINGS
    assert EVIDENCE_VALIDATOR_ID == "assurance.execution.validator.evidence.v1"
    assert_production_inventory_unbound()
