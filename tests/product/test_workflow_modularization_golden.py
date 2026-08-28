from __future__ import annotations

from pathlib import Path

from graph_engine.canonical import canonical_json_bytes

from tests.product.graph_inventory import (
    EXPECTED_OWNER_COUNTS,
    assert_workflow_module_ownership,
    load_workflow_module_ownership,
)

GOLDEN = Path(__file__).resolve().parent / "goldens" / "assurance-full-pre-modular.json"


def test_pre_modular_inventory_is_frozen() -> None:
    from assurance_product.product import load_canonical_workflow

    workflow = load_canonical_workflow()
    assert len(workflow.entrypoints) == 14
    assert len(workflow.graphs) == 52
    assert sum(len(graph.nodes) for graph in workflow.graphs.values()) == 265
    assert (
        sum(node.kind == "subgraph" for graph in workflow.graphs.values() for node in graph.nodes.values())
        == 59
    )
    assert canonical_json_bytes(workflow.model_dump(mode="json", by_alias=True, exclude_unset=True)) == (
        GOLDEN.read_bytes()
    )
    ownership = load_workflow_module_ownership()
    assert_workflow_module_ownership(workflow, ownership)
    counts = {owner: len(module["graphs"]) for owner, module in ownership["owners"].items()}
    assert counts == EXPECTED_OWNER_COUNTS
