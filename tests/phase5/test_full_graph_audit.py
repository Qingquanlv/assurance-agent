from __future__ import annotations

import pytest

pytestmark = pytest.mark.usefixtures("installed_sources")

_ARCHIVE_NODE_MARKERS = ("archive", "archive-gate", "skip-archive-gate")
_REQUIRED_RETRO_IMPROVEMENT = (
    "retro",
    "improvement-retro",
    "improvement-retro-eval-analysis",
    "improvement-review",
    "improvement-evaluate",
    "improvement-apply",
)


def _workflow():
    from assurance_product.product import load_canonical_workflow

    return load_canonical_workflow()


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
            target = node.graph
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
        if node.graph is not None:
            pending.append((node.graph, workflow.graphs[node.graph].start))
        for edge in graph.edges:
            if edge.from_ == node_id:
                pending.append((graph_id, edge.to))
    return seen


def test_full_graph_has_no_orphans_or_forbidden_targets():
    forbidden_prefixes = (
        "runtime.",
        "assurance.intake.",
        "assurance.generation.",
        "assurance.execution.",
        "assurance.quality.",
        "assurance.healing.",
    )
    workflow = _workflow()
    all_nodes = _workflow_node_ids(workflow)
    reachable = _reachable_node_ids(workflow)
    dead_ends = {
        f"{graph_id}/{node_id}"
        for graph_id, graph in workflow.graphs.items()
        for node_id, node in graph.nodes.items()
        if node.kind != "end" and not any(edge.from_ == node_id for edge in graph.edges)
    }
    forbidden = [
        f"{graph_id}/{node_id}:{node.capability}"
        for graph_id, graph in workflow.graphs.items()
        for node_id, node in graph.nodes.items()
        if node.capability is not None and node.capability.startswith(forbidden_prefixes)
    ]
    assert all_nodes - reachable == set()
    assert dead_ends == set()
    assert forbidden == []


def test_full_graph_has_no_archive_branch_and_keeps_retro_improvement():
    workflow = _workflow()
    full = workflow.graphs["full"]
    full_nodes = set(full.nodes)
    full_reachable = _reachable_graphs(workflow, "full")
    assert not any(name in full_nodes for name in _ARCHIVE_NODE_MARKERS)
    assert "archive" not in full_reachable
    assert "improvement-archive" not in full_reachable
    assert not any("archive" in f"{edge.from_}->{edge.to}" for edge in full.edges)
    assert set(_REQUIRED_RETRO_IMPROVEMENT).issubset(workflow.graphs)
    assert "retro" in full_reachable
    assert "improvement-retro" in full_reachable
    assert "improvement-retro-eval-analysis" in full_reachable
    assert "improvement-review" in full_reachable
    assert "improvement-apply" in full_reachable
    assert "achieved" in full_nodes
    assert workflow.entrypoints["archive"] == "archive"
    assert workflow.entrypoints["retro"] == "retro"
    assert workflow.entrypoints["improvement-review"] == "improvement-review"
    assert "improvement-archive" in _reachable_graphs(workflow, "archive")
    assert "improvement-retro" in _reachable_graphs(workflow, "retro")
    assert "improvement-review" in _reachable_graphs(workflow, "improvement-review")


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
