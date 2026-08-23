from __future__ import annotations

import pytest

from graph_engine.graph.input_projection import (
    InputProjectionDef,
    ObjectProjection,
    RootPointerProjection,
    TupleProjection,
)

from tests.phase5.graph_inventory import workflow_node_ids

PUBLIC_ENTRYPOINTS = {
    "full",
    "intake",
    "case",
    "execute",
    "archive",
    "retro",
    "issue-review",
    "issue-analyze",
    "issue-reconcile",
    "improvement-review",
    "improvement-evaluate",
    "improvement-export",
    "improvement-apply",
    "improvement-rollback",
}
_INTENDED_GRAPHS = {
    "intake": frozenset({"entry", "intake", "explore", "case-design", "case-review"}),
    "case": frozenset({"case", "case-design", "case-review"}),
    "execute": frozenset(
        {
            "execute",
            "generation",
            "generation-api",
            "generation-api-plan",
            "generation-api-plan-review",
            "generation-api-codegen",
            "generation-api-codegen-fix",
            "generation-e2e",
            "generation-e2e-plan",
            "generation-e2e-plan-review",
            "generation-e2e-codegen",
            "generation-e2e-codegen-fix",
            "generation-fuzz",
            "generation-fuzz-plan",
            "generation-fuzz-plan-review",
            "generation-fuzz-codegen",
            "generation-performance",
            "generation-performance-plan",
            "generation-performance-plan-review",
            "generation-performance-codegen",
            "execution-execute",
            "execution-run",
            "quality",
            "quality-fact-baseline",
            "quality-inspect",
            "quality-issue-triage",
            "quality-issue-analysis",
            "healing-fix-proposal",
            "healing-coverage-repair",
            "quality-report",
        }
    ),
    "archive": frozenset({"archive", "improvement-archive"}),
    "issue-review": frozenset({"issue-review", "quality-issue-triage"}),
    "issue-analyze": frozenset({"issue-analyze", "quality-issue-analysis"}),
    "issue-reconcile": frozenset({"issue-reconcile", "quality-issue-analysis"}),
}
_INTAKE_GRAPHS = frozenset({"entry", "intake", "explore", "case-design", "case-review", "case"})
_EMPTY_FAMILY_ENTRYPOINTS = (
    "intake",
    "case",
    "archive",
    "retro",
    "issue-review",
    "issue-analyze",
    "issue-reconcile",
    "improvement-review",
    "improvement-evaluate",
    "improvement-export",
    "improvement-apply",
    "improvement-rollback",
)

pytestmark = pytest.mark.usefixtures("installed_sources")


def test_public_entrypoints_are_exact(compiled_product_workflow):
    assert set(compiled_product_workflow.entrypoints) == PUBLIC_ENTRYPOINTS


@pytest.mark.parametrize("entrypoint", sorted(PUBLIC_ENTRYPOINTS))
def test_entrypoint_starts_with_typed_root_input(compiled_product_workflow, entrypoint):
    graph_id = compiled_product_workflow.entrypoints[entrypoint]
    graph = compiled_product_workflow.graphs[graph_id]
    assert graph.start in graph.nodes
    assert _reachable_has_root_pointer(compiled_product_workflow, graph_id, graph.start)


@pytest.mark.parametrize("entrypoint", sorted(_INTENDED_GRAPHS))
def test_entrypoint_reaches_only_its_intended_subgraph(compiled_product_workflow, entrypoint):
    reached = _reachable_graphs(compiled_product_workflow, entrypoint)
    assert reached == _INTENDED_GRAPHS[entrypoint]
    if entrypoint == "execute":
        assert reached.isdisjoint(_INTAKE_GRAPHS)
        assert "improvement-archive" not in reached
        assert "retro" not in reached


@pytest.mark.parametrize("entrypoint", _EMPTY_FAMILY_ENTRYPOINTS)
def test_new_empty_family_entrypoints_accept_empty_selection(entrypoint):
    from assurance_product.models import ProductInputV1
    from tests.phase5.test_product_input import valid_product_input

    ProductInputV1.model_validate(valid_product_input()).validate_for_entrypoint(entrypoint)


def _reachable_graphs(workflow, entrypoint: str) -> set[str]:
    start_graph = workflow.entrypoints[entrypoint]
    pending = [start_graph]
    seen: set[str] = set()
    while pending:
        graph_id = pending.pop()
        if graph_id in seen:
            continue
        seen.add(graph_id)
        for node in workflow.graphs[graph_id].nodes.values():
            target = node.definition.graph
            if target is not None and target not in seen:
                pending.append(target)
    return seen


def _reachable_has_root_pointer(workflow, graph_id: str, start: str) -> bool:
    pending = [(graph_id, start)]
    seen: set[tuple[str, str]] = set()
    while pending:
        current_graph, node_id = pending.pop()
        key = (current_graph, node_id)
        if key in seen:
            continue
        seen.add(key)
        graph = workflow.graphs[current_graph]
        node = graph.nodes[node_id]
        if _projection_has_root_pointer(node.definition.input_projection):
            return True
        if node.definition.graph is not None:
            child = workflow.graphs[node.definition.graph]
            pending.append((node.definition.graph, child.start))
        for edge in graph.edges:
            if edge.from_ == node_id:
                pending.append((current_graph, edge.to))
    return False


def _projection_has_root_pointer(projection: InputProjectionDef | None) -> bool:
    if projection is None:
        return False
    if isinstance(projection, RootPointerProjection):
        return True
    if isinstance(projection, ObjectProjection):
        return any(_projection_has_root_pointer(field) for field in projection.fields.values())
    if isinstance(projection, TupleProjection):
        return any(_projection_has_root_pointer(item) for item in projection.items)
    return False


def test_inventory_records_every_public_entrypoint(compiled_product_workflow):
    from tests.phase5.graph_inventory import load_graph_inventory

    inventory = load_graph_inventory()
    assert set(inventory["entrypoints"]) == PUBLIC_ENTRYPOINTS
    recorded = set(inventory["nodes"])
    assert recorded == workflow_node_ids(compiled_product_workflow)
    for entrypoint in PUBLIC_ENTRYPOINTS:
        assert inventory["entrypoints"][entrypoint]["nodes"]
        assert inventory["entrypoints"][entrypoint]["edges"]
