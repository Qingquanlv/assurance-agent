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
    TupleProjection,
)

from tests.phase5.conformance import ALL_BINDING_IDS, load_yaml

INVENTORY_PATH = Path(__file__).resolve().parents[2] / (
    ".superpowers/sdd/2026-08-22-pure-graph-engine-phase5-assurance-product-assembly/graph-inventory.yaml"
)
_AGENT_PREFIX = "assurance.product.agent."
_FORBIDDEN_PREFIXES = ("runtime.", "assurance.intake.", "assurance.generation.")
GENERATION_FAMILIES = ("api", "e2e", "fuzz", "performance")


def generation_prepare_ids() -> tuple[str, ...]:
    from assurance_product.models import PREPARE_IDS

    return tuple(prepare_id for prepare_id in PREPARE_IDS if prepare_id.startswith("assurance.generation."))


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


def assert_closed_agent_aliases(compiled_product_workflow: CompiledWorkflow) -> None:
    capabilities = graph_capability_ids(compiled_product_workflow)
    if not capabilities.issubset(ALL_BINDING_IDS):
        extra = sorted(capabilities - set(ALL_BINDING_IDS))
        raise AssertionError(f"graph capabilities are outside the frozen 99 aliases: {extra}")
    forbidden = sorted(
        capability
        for capability in capabilities
        if capability.startswith(_FORBIDDEN_PREFIXES) or not capability.startswith(_AGENT_PREFIX)
    )
    if forbidden:
        raise AssertionError(f"graph references a direct runtime or Phase 4 capability: {forbidden}")


def load_graph_inventory() -> dict[str, Any]:
    return load_yaml(INVENTORY_PATH)


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
