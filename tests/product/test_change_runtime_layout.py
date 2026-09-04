from __future__ import annotations


def test_loaded_canonical_workflow_binds_agent_execution_contracts(opencode_composition) -> None:
    from assurance_product.agent_contracts import AGENT_EXECUTION_CONTRACTS
    from graph_engine.composition import CapabilityBindingEntry

    composition = opencode_composition
    bindings = {
        key: value
        for key, value in composition.registries.capabilities.entries.items()
        if isinstance(value, CapabilityBindingEntry)
    }
    assert set(bindings) == set(AGENT_EXECUTION_CONTRACTS)
    for contract_id, contract in AGENT_EXECUTION_CONTRACTS.items():
        assert bindings[contract_id].contract_id == contract.contract_id
        assert contract.resources.writes
