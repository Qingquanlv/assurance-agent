from __future__ import annotations

import pytest

from assurance_product.graphs.revisions import ENTRYPOINT_CONTRACTS
from assurance_product.models import PRODUCT_ENTRYPOINTS

pytestmark = pytest.mark.usefixtures("installed_sources")

_ARCHIVE_ENTRYPOINTS = frozenset({"archive"})
_IMPROVEMENT_ENTRYPOINTS = frozenset(
    {
        "retro",
        "improvement-review",
        "improvement-evaluate",
        "improvement-export",
        "improvement-apply",
        "improvement-rollback",
    }
)


def test_full_graph_has_no_orphans_or_forbidden_targets() -> None:
    assert set(ENTRYPOINT_CONTRACTS) == set(PRODUCT_ENTRYPOINTS)
    assert len(ENTRYPOINT_CONTRACTS) == 14
    forbidden = [
        name
        for name, contract in ENTRYPOINT_CONTRACTS.items()
        if any(part.startswith("runtime.") for part in (name, contract.name))
    ]
    assert forbidden == []
    assert set(ENTRYPOINT_CONTRACTS) - set(PRODUCT_ENTRYPOINTS) == set()


def test_full_graph_has_no_archive_branch_and_keeps_retro_improvement() -> None:
    assert "full" in ENTRYPOINT_CONTRACTS
    assert "archive" in ENTRYPOINT_CONTRACTS
    assert "retro" in ENTRYPOINT_CONTRACTS
    assert "improvement-review" in ENTRYPOINT_CONTRACTS
    assert "improvement-apply" in ENTRYPOINT_CONTRACTS
    assert _ARCHIVE_ENTRYPOINTS.isdisjoint(_IMPROVEMENT_ENTRYPOINTS)
    assert "achieved" not in ENTRYPOINT_CONTRACTS
    assert ENTRYPOINT_CONTRACTS["archive"].name == "archive"
    assert ENTRYPOINT_CONTRACTS["retro"].name == "retro"
    assert ENTRYPOINT_CONTRACTS["improvement-review"].name == "improvement-review"


def test_python_roots_are_the_product_application_surface() -> None:
    from tests.product.composition_harness import SHADOW_VALIDATOR_CLONE_ID

    assert set(ENTRYPOINT_CONTRACTS) == set(PRODUCT_ENTRYPOINTS)
    assert len(ENTRYPOINT_CONTRACTS) == 14
    assert SHADOW_VALIDATOR_CLONE_ID not in ENTRYPOINT_CONTRACTS
    for contract in ENTRYPOINT_CONTRACTS.values():
        assert contract.input_schema_digest
        assert contract.output_schema_digest
        assert contract.state_schema_digest


def test_factory_composition_has_no_leftover_compiled_workflow(opencode_composition) -> None:
    composition = opencode_composition
    assert composition.manifest.graph_factory_symbol
    assert composition.lock.schema_version == "3"
    assert not hasattr(composition, "audit_full_graph")
    assert not hasattr(composition, "workflow")
