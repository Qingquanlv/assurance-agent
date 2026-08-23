from __future__ import annotations

import pytest

from tests.phase5.composition_harness import request_for
from tests.phase5.graph_inventory import (
    INVENTORY_PATH,
    assert_closed_agent_aliases,
    collect_agent_triplets,
    expected_triplet_aliases,
    graph_capability_ids,
    load_graph_inventory,
    workflow_edge_ids,
    workflow_node_ids,
)

_SLICE_GRAPHS = frozenset({"entry", "intake", "explore", "case-design", "case-review", "case"})
_SLICE_PREPARE_IDS = (
    "assurance.intake.intake.prepare",
    "assurance.intake.explore.prepare",
    "assurance.intake.case-design.prepare",
    "assurance.intake.case-review.prepare",
)
_DEFERRED_SHAPES = (
    "generation",
    "execution",
    "quality",
    "healing",
    "archive",
    "retro",
    "improvement",
)

pytestmark = pytest.mark.usefixtures("installed_sources")


@pytest.fixture
def compiled_product_workflow(installed_sources):
    from assurance_product.product import resolve_assurance_composition

    return resolve_assurance_composition(request_for("opencode", installed_sources)).workflow


def test_canonical_workflow_is_loaded_from_yaml():
    from assurance_product.product import load_canonical_workflow

    workflow = load_canonical_workflow()
    assert workflow.name == "assurance"
    assert "intake" in workflow.entrypoints
    assert "case" in workflow.entrypoints
    assert set(workflow.graphs) >= _SLICE_GRAPHS
    assert not any(name.startswith(_DEFERRED_SHAPES) for name in workflow.graphs)


def test_every_agent_node_is_one_closed_triplet(compiled_product_workflow):
    triplets = collect_agent_triplets(compiled_product_workflow)
    assert triplets
    assert {triplet.prepare_id for triplet in triplets} == set(_SLICE_PREPARE_IDS)
    for triplet in triplets:
        assert triplet.aliases == expected_triplet_aliases(triplet.prepare_id)
        assert triplet.execute_input_from == triplet.prepare_node
        assert triplet.finalize_input_from == triplet.execute_node
    assert_closed_agent_aliases(compiled_product_workflow)


@pytest.mark.parametrize("adapter", ["opencode", "cursor"])
def test_workflow_compiles_under_both_product_providers(adapter, installed_sources):
    from assurance_product.product import (
        AssuranceCursorProductProvider,
        AssuranceOpenCodeProductProvider,
        load_canonical_workflow,
        resolve_assurance_composition,
    )

    composition = resolve_assurance_composition(request_for(adapter, installed_sources))
    workflow = load_canonical_workflow()
    provider = AssuranceOpenCodeProductProvider if adapter == "opencode" else AssuranceCursorProductProvider
    assert provider.manifest().workflow == workflow
    assert composition.workflow.entrypoints == workflow.entrypoints
    assert set(composition.workflow.entrypoints) >= {"intake", "case"}
    capabilities = graph_capability_ids(composition.workflow)
    assert capabilities
    assert capabilities.issubset(
        {alias for prepare_id in _SLICE_PREPARE_IDS for alias in expected_triplet_aliases(prepare_id)}
    )
    assert_closed_agent_aliases(composition.workflow)


def test_graph_inventory_records_this_slice(compiled_product_workflow):
    inventory = load_graph_inventory()
    recorded_nodes = set(inventory["nodes"])
    recorded_edges = set(inventory["edges"])
    recorded_aliases = set(inventory.get("aliases", []))
    assert recorded_nodes == workflow_node_ids(compiled_product_workflow)
    assert recorded_edges == workflow_edge_ids(compiled_product_workflow)
    assert recorded_aliases == graph_capability_ids(compiled_product_workflow)
    assert INVENTORY_PATH.is_file()
    for entrypoint in ("intake", "case"):
        assert inventory["entrypoints"][entrypoint]["nodes"]
        assert inventory["entrypoints"][entrypoint]["edges"]
