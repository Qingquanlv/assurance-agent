from __future__ import annotations

import pytest

from graph_engine.graph.input_projection import (
    GraphInputPointerProjection,
    InputProjectionDef,
    ObjectProjection,
    RootPointerProjection,
    TupleProjection,
)

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


def _feature_graph(feature: str, local: str) -> str:
    return f"assurance.{feature}.workflow.graph.{local}"


def _product_graph(name: str) -> str:
    return f"assurance.product.workflow.graph.product-{name}"


_INTENDED_GRAPHS = {
    "intake": frozenset(
        {
            _product_graph("intake"),
            _feature_graph("intake", "entry"),
            _feature_graph("intake", "intake"),
            _feature_graph("intake", "explore"),
            _feature_graph("intake", "case-design"),
            _feature_graph("intake", "case-review"),
        }
    ),
    "case": frozenset(
        {
            _product_graph("case"),
            _feature_graph("intake", "case"),
            _feature_graph("intake", "case-design"),
            _feature_graph("intake", "case-review"),
        }
    ),
    "execute": frozenset(
        {
            _product_graph("execute"),
            _feature_graph("generation", "generation"),
            _feature_graph("generation", "generation-api"),
            _feature_graph("generation", "generation-api-plan"),
            _feature_graph("generation", "generation-api-plan-review"),
            _feature_graph("generation", "generation-api-codegen"),
            _feature_graph("generation", "generation-api-codegen-fix"),
            _feature_graph("generation", "generation-e2e"),
            _feature_graph("generation", "generation-e2e-plan"),
            _feature_graph("generation", "generation-e2e-plan-review"),
            _feature_graph("generation", "generation-e2e-codegen"),
            _feature_graph("generation", "generation-e2e-codegen-fix"),
            _feature_graph("generation", "generation-fuzz"),
            _feature_graph("generation", "generation-fuzz-plan"),
            _feature_graph("generation", "generation-fuzz-plan-review"),
            _feature_graph("generation", "generation-fuzz-codegen"),
            _feature_graph("generation", "generation-performance"),
            _feature_graph("generation", "generation-performance-plan"),
            _feature_graph("generation", "generation-performance-plan-review"),
            _feature_graph("generation", "generation-performance-codegen"),
            _feature_graph("execution", "execution-execute"),
            _feature_graph("execution", "execution-run"),
            _feature_graph("quality", "quality"),
            _feature_graph("quality", "quality-fact-baseline"),
            _feature_graph("quality", "quality-inspect"),
            _feature_graph("quality", "quality-issue-triage"),
            _feature_graph("quality", "quality-issue-analysis"),
            _feature_graph("quality", "issue-review"),
            _feature_graph("quality", "issue-analyze"),
            _feature_graph("healing", "healing-fix-proposal"),
            _feature_graph("healing", "healing-coverage-repair"),
            _feature_graph("quality", "quality-report"),
        }
    ),
    "archive": frozenset(
        {
            _product_graph("archive"),
            _feature_graph("improvement", "archive"),
            _feature_graph("improvement", "improvement-archive"),
        }
    ),
    "issue-review": frozenset(
        {
            _product_graph("issue-review"),
            _feature_graph("quality", "issue-review"),
            _feature_graph("quality", "quality-issue-triage"),
        }
    ),
    "issue-analyze": frozenset(
        {
            _product_graph("issue-analyze"),
            _feature_graph("quality", "issue-analyze"),
            _feature_graph("quality", "quality-issue-analysis"),
        }
    ),
    "issue-reconcile": frozenset(
        {
            _product_graph("issue-reconcile"),
            _feature_graph("quality", "issue-reconcile"),
            _feature_graph("quality", "quality-issue-analysis"),
        }
    ),
}
_INTAKE_GRAPHS = frozenset(
    {
        _feature_graph("intake", "entry"),
        _feature_graph("intake", "intake"),
        _feature_graph("intake", "explore"),
        _feature_graph("intake", "case-design"),
        _feature_graph("intake", "case-review"),
        _feature_graph("intake", "case"),
    }
)
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
    assert set(compiled_product_workflow.entrypoints) == {
        "intake",
        "case",
        "full",
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
    assert set(compiled_product_workflow.entrypoints) == PUBLIC_ENTRYPOINTS
    assert all(
        f"assurance.product.workflow.graph.product-{name}" in compiled_product_workflow.graphs
        for name in compiled_product_workflow.entrypoints
    )
    assert all(
        compiled_product_workflow.entrypoints[name] == f"assurance.product.workflow.graph.product-{name}"
        for name in compiled_product_workflow.entrypoints
    )


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
        assert _feature_graph("improvement", "improvement-archive") not in reached
        assert _feature_graph("improvement", "retro") not in reached


@pytest.mark.parametrize("entrypoint", _EMPTY_FAMILY_ENTRYPOINTS)
def test_new_empty_family_entrypoints_accept_empty_selection(entrypoint):
    from assurance_product.models import ProductInputV1
    from tests.product.test_product_input import valid_product_input

    case_delta_paths = (
        ("qa/changes/CH-DEMO-001/cases/system/dept/case.yaml",) if entrypoint in {"intake", "case"} else ()
    )
    ProductInputV1.model_validate(
        valid_product_input(case_delta_paths=case_delta_paths)
    ).validate_for_entrypoint(entrypoint)


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
    if isinstance(projection, RootPointerProjection | GraphInputPointerProjection):
        return True
    if isinstance(projection, ObjectProjection):
        return any(_projection_has_root_pointer(field) for field in projection.fields.values())
    if isinstance(projection, TupleProjection):
        return any(_projection_has_root_pointer(item) for item in projection.items)
    return False


def test_inventory_records_every_public_entrypoint(compiled_product_workflow):
    assert set(compiled_product_workflow.entrypoints) == PUBLIC_ENTRYPOINTS
    for entrypoint, graph_id in compiled_product_workflow.entrypoints.items():
        graph = compiled_product_workflow.graphs[graph_id]
        assert graph.nodes
        assert graph.edges
        assert graph_id == f"assurance.product.workflow.graph.product-{entrypoint}"
