from __future__ import annotations

import pytest

from assurance_product.models import PRODUCT_ENTRYPOINTS

pytestmark = pytest.mark.usefixtures("installed_sources")


def test_full_graph_has_no_orphans_or_forbidden_targets() -> None:
    from assurance_product.graphs.factory import entrypoint_contracts

    contracts = entrypoint_contracts()
    assert set(contracts) == set(PRODUCT_ENTRYPOINTS)
    assert len(contracts) == 7
    forbidden = [
        name
        for name, contract in contracts.items()
        if any(part.startswith("runtime.") for part in (name, contract.name))
    ]
    assert forbidden == []
    assert set(contracts) - set(PRODUCT_ENTRYPOINTS) == set()


def test_full_graph_has_no_archive_branch_and_keeps_retro_improvement() -> None:
    from assurance_product.graphs.factory import entrypoint_contracts

    contracts = entrypoint_contracts()
    assert "full" in contracts
    assert "retro" in contracts
    assert "archive" not in contracts
    assert "improvement-review" not in contracts
    assert "improvement-apply" not in contracts
    assert "achieved" not in contracts
    assert contracts["full"].name == "full"
    assert contracts["retro"].name == "retro"


def test_python_roots_are_the_product_application_surface() -> None:
    from assurance_product.graphs.factory import entrypoint_contracts
    from tests.product.composition_harness import SHADOW_VALIDATOR_CLONE_ID

    contracts = entrypoint_contracts()
    assert set(contracts) == set(PRODUCT_ENTRYPOINTS)
    assert len(contracts) == 7
    assert SHADOW_VALIDATOR_CLONE_ID not in contracts
    for contract in contracts.values():
        assert contract.input_schema_digest
        assert contract.output_schema_digest
        assert contract.state_schema_digest


def test_factory_composition_has_no_leftover_compiled_workflow(opencode_composition) -> None:
    composition = opencode_composition
    assert composition.manifest.graph_factory_symbol
    assert composition.lock.schema_version == "3"
    assert not hasattr(composition, "audit_full_graph")
    assert not hasattr(composition, "workflow")
