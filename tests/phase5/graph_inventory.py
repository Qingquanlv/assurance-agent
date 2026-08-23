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
