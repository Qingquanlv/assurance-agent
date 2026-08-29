from __future__ import annotations

import pytest

pytestmark = pytest.mark.usefixtures("installed_sources")

_ARCHIVE_NODE_MARKERS = ("archive", "archive-gate", "skip-archive-gate")
_PRODUCT_PREFIX = "assurance.product.workflow.graph."
_IMPROVEMENT_PREFIX = "assurance.improvement.workflow.graph."


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
            target = node.definition.graph if hasattr(node, "definition") else node.graph
            if target is not None and target not in seen:
                pending.append(target)
    return seen


def _workflow_node_ids(workflow) -> set[str]:
    return {f"{graph_id}/{node_id}" for graph_id, graph in workflow.graphs.items() for node_id in graph.nodes}


def _reachable_node_ids(workflow) -> set[str]:
    pending: list[tuple[str, str]] = []
    for graph_id in workflow.entrypoints.values():
        pending.append((graph_id, workflow.graphs[graph_id].start))
    seen: set[str] = set()
    while pending:
        graph_id, node_id = pending.pop()
        key = f"{graph_id}/{node_id}"
        if key in seen:
            continue
        seen.add(key)
        graph = workflow.graphs[graph_id]
        node = graph.nodes[node_id]
        target = node.definition.graph if hasattr(node, "definition") else node.graph
        if target is not None:
            pending.append((target, workflow.graphs[target].start))
        edges = graph.edges if hasattr(graph, "edges") else ()
        outgoing = node.outgoing if hasattr(node, "outgoing") else ()
        if outgoing:
            for edge in outgoing:
                pending.append((graph_id, edge.to))
        else:
            for edge in edges:
                if edge.from_ == node_id:
                    pending.append((graph_id, edge.to))
    return seen


def test_full_graph_has_no_orphans_or_forbidden_targets(compiled_product_workflow):
    forbidden_prefixes = ("runtime.",)
    workflow = compiled_product_workflow
    all_nodes = _workflow_node_ids(workflow)
    reachable = _reachable_node_ids(workflow)
    dead_ends = {
        f"{graph_id}/{node_id}"
        for graph_id, graph in workflow.graphs.items()
        for node_id, node in graph.nodes.items()
        if node.definition.kind not in {"end", "interrupt"} and not node.outgoing
    }
    forbidden = [
        f"{graph_id}/{node_id}:{node.definition.capability}"
        for graph_id, graph in workflow.graphs.items()
        for node_id, node in graph.nodes.items()
        if node.definition.capability is not None
        and node.definition.capability.startswith(forbidden_prefixes)
    ]
    assert all_nodes - reachable == set()
    assert dead_ends == set()
    assert forbidden == []


def test_full_graph_has_no_archive_branch_and_keeps_retro_improvement(compiled_product_workflow):
    workflow = compiled_product_workflow
    full = workflow.graphs[f"{_PRODUCT_PREFIX}product-full"]
    full_nodes = set(full.nodes)
    full_reachable = _reachable_graphs(workflow, "full")
    assert not any(name in full_nodes for name in _ARCHIVE_NODE_MARKERS)
    assert f"{_IMPROVEMENT_PREFIX}archive" not in full_reachable
    assert f"{_IMPROVEMENT_PREFIX}improvement-archive" not in full_reachable
    assert not any("archive" in f"{edge.from_}->{edge.to}" for edge in full.edges)
    assert f"{_IMPROVEMENT_PREFIX}retro" in workflow.graphs
    assert f"{_IMPROVEMENT_PREFIX}improvement-retro" in workflow.graphs
    assert f"{_IMPROVEMENT_PREFIX}improvement-retro-eval-analysis" in workflow.graphs
    assert f"{_IMPROVEMENT_PREFIX}improvement-review" in workflow.graphs
    assert f"{_IMPROVEMENT_PREFIX}improvement-apply" in workflow.graphs
    assert f"{_IMPROVEMENT_PREFIX}retro" in full_reachable
    assert f"{_IMPROVEMENT_PREFIX}improvement-retro" in full_reachable
    assert f"{_IMPROVEMENT_PREFIX}improvement-retro-eval-analysis" in full_reachable
    assert f"{_IMPROVEMENT_PREFIX}improvement-review" in full_reachable
    assert f"{_IMPROVEMENT_PREFIX}improvement-apply" in full_reachable
    assert "achieved" in full_nodes
    assert workflow.entrypoints["archive"] == f"{_PRODUCT_PREFIX}product-archive"
    assert workflow.entrypoints["retro"] == f"{_PRODUCT_PREFIX}product-retro"
    assert workflow.entrypoints["improvement-review"] == f"{_PRODUCT_PREFIX}product-improvement-review"
    assert f"{_IMPROVEMENT_PREFIX}improvement-archive" in _reachable_graphs(workflow, "archive")
    assert f"{_IMPROVEMENT_PREFIX}improvement-retro" in _reachable_graphs(workflow, "retro")
    assert f"{_IMPROVEMENT_PREFIX}improvement-review" in _reachable_graphs(workflow, "improvement-review")


def test_audit_result_fields_are_tuples(compiled_for):
    from assurance_product.product import audit_full_graph

    try:
        compiled = compiled_for("opencode")
    except Exception:
        pytest.skip("composition snapshot cannot preload an already-imported product module")
    audit = audit_full_graph(compiled.workflow, compiled.composition)
    assert audit.unreachable_nodes == tuple(audit.unreachable_nodes)
    assert audit.dead_ends == tuple(audit.dead_ends)
    assert audit.forbidden_direct_targets == tuple(audit.forbidden_direct_targets)
    assert audit.missing_bindings == tuple(audit.missing_bindings)
    assert audit.uninventoried_nodes == tuple(audit.uninventoried_nodes)
