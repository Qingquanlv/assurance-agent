from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from graph_engine.graph.compiler import CompiledNode, CompiledWorkflow
from graph_engine.graph.input_projection import (
    InputProjectionDef,
    ObjectProjection,
    PredecessorPointerProjection,
    PredecessorValueProjection,
    RootPointerProjection,
    TupleProjection,
)
from graph_engine.graph.schema import WorkflowDef

from tests.product.conformance import ALL_BINDING_IDS, load_yaml

INVENTORY_PATH = Path(__file__).resolve().parents[2] / (
    "packages/products/assurance-product/assurance_product/resources/graph-inventory.yaml"
)
_AGENT_PREFIX = "assurance.product.agent."
_FORBIDDEN_PREFIXES = (
    "runtime.",
    "assurance.intake.",
    "assurance.generation.",
    "assurance.execution.",
    "assurance.quality.",
    "assurance.healing.",
)
_PHASE4_IMPROVEMENT_AGENT_PREPARES = (
    "assurance.improvement.archive.prepare",
    "assurance.improvement.improvement-review.prepare",
    "assurance.improvement.retro-eval-analysis.prepare",
    "assurance.improvement.retro-issue-analysis.prepare",
    "assurance.improvement.retro-workflow-analysis.prepare",
    "assurance.improvement.retro.prepare",
)
_PHASE4_IMPROVEMENT_AGENT_IDS = frozenset(
    f"{prepare_id.removesuffix('.prepare')}{suffix}"
    for prepare_id in _PHASE4_IMPROVEMENT_AGENT_PREPARES
    for suffix in (".prepare", ".finalize")
)
GENERATION_FAMILIES = ("api", "e2e", "fuzz", "performance")


def generation_prepare_ids() -> tuple[str, ...]:
    from assurance_product.models import PREPARE_IDS

    return tuple(prepare_id for prepare_id in PREPARE_IDS if prepare_id.startswith("assurance.generation."))


def execution_quality_prepare_ids() -> tuple[str, ...]:
    from assurance_product.models import PREPARE_IDS

    return tuple(
        prepare_id
        for prepare_id in PREPARE_IDS
        if prepare_id.startswith(("assurance.execution.", "assurance.quality.", "assurance.healing."))
    )


@dataclass(frozen=True)
class AgentTriplet:
    prepare_id: str
    aliases: tuple[str, str, str]
    graph_id: str
    prepare_node: str
    execute_node: str
    finalize_node: str
    execute_input_from: str | None
    finalize_input_from: str | None


def expected_triplet_aliases(prepare_id: str) -> tuple[str, str, str]:
    from assurance_product.models import alias_ids_for_prepare

    return alias_ids_for_prepare(prepare_id)


def collect_agent_triplets(compiled_product_workflow: CompiledWorkflow) -> tuple[AgentTriplet, ...]:
    triplets: list[AgentTriplet] = []
    for graph_id, graph in compiled_product_workflow.graphs.items():
        grouped: dict[str, dict[str, CompiledNode]] = {}
        for node in graph.nodes.values():
            capability = node.definition.capability
            if capability is None or not capability.startswith(_AGENT_PREFIX):
                continue
            if capability.endswith(".prepare"):
                stem = capability.removesuffix(".prepare")
                grouped.setdefault(stem, {})["prepare"] = node
            elif capability.endswith(".execute"):
                stem = capability.removesuffix(".execute")
                grouped.setdefault(stem, {})["execute"] = node
            elif capability.endswith(".finalize"):
                stem = capability.removesuffix(".finalize")
                grouped.setdefault(stem, {})["finalize"] = node
            else:
                raise AssertionError(f"agent capability is not a triplet phase: {capability}")
        for stem, parts in grouped.items():
            missing = {"prepare", "execute", "finalize"} - set(parts)
            if missing:
                raise AssertionError(
                    f"incomplete agent triplet {stem} in {graph_id}: missing {sorted(missing)}"
                )
            prepare = parts["prepare"]
            execute = parts["execute"]
            finalize = parts["finalize"]
            prepare_id = f"assurance.{stem.removeprefix(_AGENT_PREFIX)}.prepare"
            triplets.append(
                AgentTriplet(
                    prepare_id=prepare_id,
                    aliases=expected_triplet_aliases(prepare_id),
                    graph_id=graph_id,
                    prepare_node=prepare.node_id,
                    execute_node=execute.node_id,
                    finalize_node=finalize.node_id,
                    execute_input_from=_direct_predecessor(execute.definition.input_projection),
                    finalize_input_from=_direct_predecessor(finalize.definition.input_projection),
                )
            )
    return tuple(triplets)


def graph_capability_ids(compiled_product_workflow: CompiledWorkflow) -> frozenset[str]:
    return frozenset(
        node.definition.capability
        for graph in compiled_product_workflow.graphs.values()
        for node in graph.nodes.values()
        if node.definition.capability is not None
    )


def _is_allowed_phase4_operation(capability: str) -> bool:
    return capability.startswith("assurance.improvement.") and capability not in _PHASE4_IMPROVEMENT_AGENT_IDS


def assert_closed_agent_aliases(compiled_product_workflow: CompiledWorkflow) -> None:
    capabilities = graph_capability_ids(compiled_product_workflow)
    agent_ids = {capability for capability in capabilities if capability.startswith(_AGENT_PREFIX)}
    operation_ids = capabilities - agent_ids
    if not agent_ids.issubset(ALL_BINDING_IDS):
        extra = sorted(agent_ids - set(ALL_BINDING_IDS))
        raise AssertionError(f"graph capabilities are outside the frozen 99 aliases: {extra}")
    forbidden = sorted(
        capability
        for capability in capabilities
        if capability.startswith(_FORBIDDEN_PREFIXES) or capability in _PHASE4_IMPROVEMENT_AGENT_IDS
    )
    if forbidden:
        raise AssertionError(f"graph references a direct runtime or Phase 4 capability: {forbidden}")
    unknown = sorted(
        capability for capability in operation_ids if not _is_allowed_phase4_operation(capability)
    )
    if unknown:
        raise AssertionError(
            f"graph references a capability outside agent aliases and Phase 4 operations: {unknown}"
        )


def load_graph_inventory() -> dict[str, Any]:
    return load_yaml(INVENTORY_PATH)


def dump_graph_inventory(compiled_product_workflow: CompiledWorkflow) -> dict[str, Any]:
    entrypoints: dict[str, dict[str, list[str]]] = {}
    for name, graph_id in compiled_product_workflow.entrypoints.items():
        nodes, edges, aliases = _entrypoint_closure(compiled_product_workflow, graph_id)
        entrypoints[name] = {
            "nodes": sorted(nodes),
            "edges": sorted(edges),
            "aliases": sorted(aliases),
        }
    return {
        "entrypoints": entrypoints,
        "nodes": sorted(workflow_node_ids(compiled_product_workflow)),
        "edges": sorted(workflow_edge_ids(compiled_product_workflow)),
        "aliases": sorted(graph_capability_ids(compiled_product_workflow)),
    }


def write_graph_inventory(compiled_product_workflow: CompiledWorkflow) -> Path:
    import yaml

    document = dump_graph_inventory(compiled_product_workflow)
    INVENTORY_PATH.write_text(
        yaml.safe_dump(document, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return INVENTORY_PATH


def _entrypoint_closure(
    compiled_product_workflow: CompiledWorkflow, start_graph: str
) -> tuple[set[str], set[str], set[str]]:
    pending = [start_graph]
    seen: set[str] = set()
    nodes: set[str] = set()
    edges: set[str] = set()
    aliases: set[str] = set()
    while pending:
        graph_id = pending.pop()
        if graph_id in seen:
            continue
        seen.add(graph_id)
        graph = compiled_product_workflow.graphs[graph_id]
        for node_id, node in graph.nodes.items():
            nodes.add(f"{graph_id}/{node_id}")
            capability = node.definition.capability
            if capability is not None:
                aliases.add(capability)
            target = node.definition.graph
            if target is not None and target not in seen:
                pending.append(target)
        for edge in graph.edges:
            edges.add(f"{graph_id}:{edge.from_}->{edge.to}")
    return nodes, edges, aliases


def workflow_node_ids(compiled_product_workflow: CompiledWorkflow) -> set[str]:
    return {
        f"{graph_id}/{node_id}"
        for graph_id, graph in compiled_product_workflow.graphs.items()
        for node_id in graph.nodes
    }


def workflow_edge_ids(compiled_product_workflow: CompiledWorkflow) -> set[str]:
    edges: set[str] = set()
    for graph_id, graph in compiled_product_workflow.graphs.items():
        for edge in graph.edges:
            edges.add(f"{graph_id}:{edge.from_}->{edge.to}")
    return edges


def _direct_predecessor(projection: InputProjectionDef | None) -> str | None:
    if projection is None:
        return None
    if isinstance(projection, PredecessorValueProjection | PredecessorPointerProjection):
        return projection.predecessor
    if isinstance(projection, ObjectProjection):
        found = [
            predecessor
            for predecessor in (_direct_predecessor(field) for field in projection.fields.values())
            if predecessor is not None
        ]
        if not found:
            return None
        if len(set(found)) == 1:
            return found[0]
        return found[0]
    if isinstance(projection, TupleProjection):
        found = [
            predecessor
            for predecessor in (_direct_predecessor(child) for child in projection.items)
            if predecessor is not None
        ]
        return found[0] if found else None
    return None


OWNERSHIP_PATH = Path(__file__).resolve().parent / "fixtures" / "workflow-module-ownership.yaml"
EXPECTED_OWNER_COUNTS = {
    "intake": 6,
    "generation": 19,
    "execution": 2,
    "healing": 2,
    "quality": 9,
    "improvement": 12,
    "product": 2,
}
RELOCATION_PREFIX_NODES = (
    "intake",
    "explore",
    "case-design",
    "case-review",
    "review-pass-gate",
    "review-fix-gate",
    "review-human-gate",
    "human-review",
)


def load_workflow_module_ownership() -> dict[str, Any]:
    return load_yaml(OWNERSHIP_PATH)


def assert_workflow_module_ownership(
    workflow: WorkflowDef,
    ownership: dict[str, Any] | None = None,
) -> dict[str, Any]:
    document = load_workflow_module_ownership() if ownership is None else ownership
    owners = document["owners"]
    if set(owners) != set(EXPECTED_OWNER_COUNTS):
        raise AssertionError(f"ownership owners {sorted(owners)} != {sorted(EXPECTED_OWNER_COUNTS)}")
    assigned: dict[str, str] = {}
    for owner, module in owners.items():
        graphs = list(module["graphs"])
        if len(graphs) != EXPECTED_OWNER_COUNTS[owner]:
            raise AssertionError(
                f"{owner} owns {len(graphs)} graphs, expected {EXPECTED_OWNER_COUNTS[owner]}"
            )
        if len(set(graphs)) != len(graphs):
            raise AssertionError(f"{owner} assigns a graph more than once: {graphs}")
        for graph_id in graphs:
            if graph_id in assigned:
                raise AssertionError(f"{graph_id} assigned to both {assigned[graph_id]} and {owner}")
            assigned[graph_id] = owner
    if set(assigned) != set(workflow.graphs):
        extra = sorted(set(assigned) - set(workflow.graphs))
        missing = sorted(set(workflow.graphs) - set(assigned))
        raise AssertionError(f"ownership graph mismatch extra={extra} missing={missing}")
    _assert_export_targets(owners, assigned)
    _assert_local_subgraph_calls(workflow, document, assigned)
    _assert_exported_closures(workflow, document, assigned)
    _assert_product_to_intake_relocation(workflow, document)
    return document


def _assert_export_targets(owners: dict[str, Any], assigned: dict[str, str]) -> None:
    for owner, module in owners.items():
        exports = module.get("exports", {})
        if owner == "product":
            if exports:
                raise AssertionError("product retains full/execute and has no Feature public exports")
            continue
        if not exports:
            raise AssertionError(f"{owner} is missing public export targets")
        for export_name, export in exports.items():
            target = export["target"]
            if assigned.get(target) != owner:
                raise AssertionError(f"{owner}.{export_name} target {target} is not owned by {owner}")


def _assert_local_subgraph_calls(
    workflow: WorkflowDef,
    ownership: dict[str, Any],
    assigned: dict[str, str],
) -> None:
    expected = set(_iter_local_subgraph_calls(workflow, assigned))
    recorded = {(item["graph"], item["node"], item["target"]) for item in ownership["local_subgraph_calls"]}
    if recorded != expected:
        raise AssertionError(
            "local subgraph calls drifted: "
            f"extra={sorted(recorded - expected)} missing={sorted(expected - recorded)}"
        )


def _assert_exported_closures(
    workflow: WorkflowDef,
    ownership: dict[str, Any],
    assigned: dict[str, str],
) -> None:
    closures = ownership["exported_closures"]
    expected_keys = {
        f"{owner}.{export_name}"
        for owner, module in ownership["owners"].items()
        for export_name in module.get("exports", {})
    }
    if set(closures) != expected_keys:
        raise AssertionError(
            f"exported closures {sorted(closures)} != public exports {sorted(expected_keys)}"
        )
    for key, closure in closures.items():
        owner, export_name = key.split(".", 1)
        target = ownership["owners"][owner]["exports"][export_name]["target"]
        if closure["target"] != target:
            raise AssertionError(f"{key} closure target {closure['target']} != export target {target}")
        expected_graphs = _owned_closure(workflow, target, assigned, owner)
        if set(closure["graphs"]) != expected_graphs:
            raise AssertionError(
                f"{key} closure graphs {sorted(closure['graphs'])} != {sorted(expected_graphs)}"
            )
        expected_pointers = _root_pointers_in_graphs(workflow, expected_graphs)
        recorded_pointers = {
            (item["graph"], item["node"], item["field"], item["pointer"]) for item in closure["root_pointers"]
        }
        if recorded_pointers != expected_pointers:
            raise AssertionError(
                f"{key} root_pointers drifted: "
                f"extra={sorted(recorded_pointers - expected_pointers)} "
                f"missing={sorted(expected_pointers - recorded_pointers)}"
            )


def _assert_product_to_intake_relocation(workflow: WorkflowDef, ownership: dict[str, Any]) -> None:
    relocation = ownership["product_to_intake_relocation"]
    if relocation["source_graph"] != "full":
        raise AssertionError("relocation source_graph must be legacy full")
    if relocation["destination_export"] != "intake.prepare":
        raise AssertionError("relocation destination_export must be intake.prepare")
    if relocation["public_terminal_boundary"] != "intake.prepare":
        raise AssertionError("legacy generation continuations become the intake.prepare terminal")
    if tuple(relocation["prefix_nodes"]) != RELOCATION_PREFIX_NODES:
        raise AssertionError(
            f"relocation prefix_nodes {relocation['prefix_nodes']} != {list(RELOCATION_PREFIX_NODES)}"
        )
    full = workflow.graphs["full"]
    prefix = set(RELOCATION_PREFIX_NODES)
    expected_internal = {
        _edge_record(edge.from_, edge.to, edge.condition)
        for edge in full.edges
        if edge.from_ in prefix and edge.to in prefix
    }
    recorded_internal = {
        _edge_record(item["from"], item["to"], item.get("condition"))
        for item in relocation["prefix_internal_edges"]
    }
    if recorded_internal != expected_internal:
        raise AssertionError(
            "relocation internal edges drifted: "
            f"extra={sorted(recorded_internal - expected_internal)} "
            f"missing={sorted(expected_internal - recorded_internal)}"
        )
    expected_continuation = {
        _edge_record(edge.from_, edge.to, edge.condition)
        for edge in full.edges
        if edge.from_ in prefix and edge.to == "generation"
    }
    recorded_continuation = {
        _edge_record(item["from"], item["to"], item.get("condition"))
        for item in relocation["continuation_edges"]
    }
    if recorded_continuation != expected_continuation:
        raise AssertionError(
            "relocation continuation edges drifted: "
            f"extra={sorted(recorded_continuation - expected_continuation)} "
            f"missing={sorted(expected_continuation - recorded_continuation)}"
        )
    if not expected_continuation:
        raise AssertionError("legacy full must continue into generation")


def _iter_local_subgraph_calls(
    workflow: WorkflowDef,
    assigned: dict[str, str],
) -> tuple[tuple[str, str, str], ...]:
    calls: list[tuple[str, str, str]] = []
    for graph_id, graph in workflow.graphs.items():
        for node_id, node in graph.nodes.items():
            if node.kind != "subgraph" or node.graph is None:
                continue
            if assigned[graph_id] == assigned[node.graph]:
                calls.append((graph_id, node_id, node.graph))
    return tuple(calls)


def _owned_closure(
    workflow: WorkflowDef,
    start: str,
    assigned: dict[str, str],
    owner: str,
) -> set[str]:
    pending = [start]
    seen: set[str] = set()
    while pending:
        graph_id = pending.pop()
        if graph_id in seen or assigned[graph_id] != owner:
            continue
        seen.add(graph_id)
        for node in workflow.graphs[graph_id].nodes.values():
            if node.kind == "subgraph" and node.graph is not None and node.graph not in seen:
                pending.append(node.graph)
    return seen


def _root_pointers_in_graphs(workflow: WorkflowDef, graph_ids: set[str]) -> set[tuple[str, str, str, str]]:
    found: set[tuple[str, str, str, str]] = set()
    for graph_id in graph_ids:
        for node_id, node in workflow.graphs[graph_id].nodes.items():
            for field, pointer in _iter_root_pointers(node.input_projection):
                found.add((graph_id, node_id, field, pointer))
    return found


def _iter_root_pointers(
    projection: InputProjectionDef | None, prefix: str = ""
) -> tuple[tuple[str, str], ...]:
    if projection is None:
        return ()
    if isinstance(projection, RootPointerProjection):
        return ((prefix, projection.pointer),)
    if isinstance(projection, ObjectProjection):
        items: list[tuple[str, str]] = []
        for field, child in projection.fields.items():
            path = field if not prefix else f"{prefix}.{field}"
            items.extend(_iter_root_pointers(child, path))
        return tuple(items)
    if isinstance(projection, TupleProjection):
        items = []
        for index, child in enumerate(projection.items):
            path = f"{prefix}[{index}]" if prefix else f"[{index}]"
            items.extend(_iter_root_pointers(child, path))
        return tuple(items)
    return ()


def _edge_record(from_node: str, to_node: str, condition: str | None) -> tuple[str, str, str | None]:
    return (from_node, to_node, condition)
