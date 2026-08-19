from __future__ import annotations

from typing import Any, NoReturn, TypeVar, cast

from pydantic import BaseModel, ConfigDict, field_validator

from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.errors import GraphEngineError
from graph_engine.graph.schema import (
    EdgeDef,
    GraphDef,
    NodeDef,
    RetryPolicyDef,
    TimeoutPolicyDef,
    WorkflowDef,
)
from graph_engine.plugin_api import CapabilityRegistry


class CompileError(GraphEngineError):
    """Raised when a structural workflow cannot be compiled."""


_Key = TypeVar("_Key")
_Value = TypeVar("_Value")


class _FrozenDict(dict[_Key, _Value]):
    """A serialization-friendly immutable snapshot of a public mapping."""

    @staticmethod
    def _blocked(*_args: object, **_kwargs: object) -> NoReturn:
        raise TypeError("compiled mappings are immutable")

    __setitem__ = _blocked  # type: ignore[assignment]
    __delitem__ = _blocked  # type: ignore[assignment]
    __ior__ = _blocked  # type: ignore[assignment]
    clear = _blocked  # type: ignore[assignment]
    pop = _blocked  # type: ignore[assignment]
    popitem = _blocked  # type: ignore[assignment]
    setdefault = _blocked  # type: ignore[assignment]
    update = _blocked  # type: ignore[assignment]


_COMPILED_CONFIG = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class _CompiledModel(BaseModel):
    model_config = _COMPILED_CONFIG


class CompiledNode(_CompiledModel):
    graph_id: str
    node_id: str
    declaration_index: int
    topology_rank: int
    definition: NodeDef
    incoming: tuple[EdgeDef, ...]
    outgoing: tuple[EdgeDef, ...]


class CompiledGraph(_CompiledModel):
    graph_id: str
    max_activations: int
    start: str
    declaration_order: tuple[str, ...]
    nodes: dict[str, CompiledNode]
    edges: tuple[EdgeDef, ...]
    sccs: tuple[tuple[str, ...], ...]

    @field_validator("nodes", mode="after")
    @classmethod
    def _freeze_nodes(cls, value: dict[str, CompiledNode]) -> dict[str, CompiledNode]:
        return _FrozenDict(value)


class CompiledWorkflow(_CompiledModel):
    name: str
    entrypoints: dict[str, str]
    retry: dict[str, RetryPolicyDef]
    timeout: dict[str, TimeoutPolicyDef]
    graphs: dict[str, CompiledGraph]
    digest: str

    @field_validator("entrypoints", "retry", "timeout", "graphs", mode="after")
    @classmethod
    def _freeze_mapping(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _FrozenDict(value)


def compile_workflow(workflow: WorkflowDef, registry: CapabilityRegistry) -> CompiledWorkflow:
    _validate_workflow(workflow, registry)
    graphs = {graph_id: _compile_graph(graph_id, graph) for graph_id, graph in workflow.graphs.items()}
    compiled = CompiledWorkflow(
        name=workflow.name,
        entrypoints=workflow.entrypoints,
        retry=workflow.retry,
        timeout=workflow.timeout,
        graphs=graphs,
        digest="",
    )
    payload = cast(
        JSONValue,
        compiled.model_dump(mode="json", by_alias=True, exclude={"digest"}),
    )
    return compiled.model_copy(update={"digest": canonical_digest(payload)})


def _validate_workflow(workflow: WorkflowDef, registry: CapabilityRegistry) -> None:
    for graph_id in workflow.entrypoints.values():
        if graph_id not in workflow.graphs:
            raise CompileError(f"entrypoint references unknown graph {graph_id}")

    for graph_id, graph in workflow.graphs.items():
        if graph.start not in graph.nodes:
            raise CompileError(f"unknown start node {graph_id}/{graph.start}")

        for edge in graph.edges:
            if edge.from_ not in graph.nodes:
                raise CompileError(f"unknown edge from {graph_id}/{edge.from_}")
            if edge.to not in graph.nodes:
                raise CompileError(f"unknown edge to {graph_id}/{edge.to}")

        incoming_sources: dict[str, set[str]] = {node_id: set() for node_id in graph.nodes}
        outgoing: dict[str, list[str]] = {node_id: [] for node_id in graph.nodes}
        for edge in graph.edges:
            incoming_sources[edge.to].add(edge.from_)
            outgoing[edge.from_].append(edge.to)

        for node_id, node in graph.nodes.items():
            _validate_node_references(workflow, registry, graph_id, node_id, node)
            if node.kind == "end" and outgoing[node_id]:
                raise CompileError(f"end node {graph_id}/{node_id} has outgoing edge")
            if node.kind == "join" and node.join == "all" and len(incoming_sources[node_id]) < 2:
                raise CompileError(f"all join {graph_id}/{node_id} requires two distinct incoming sources")

        reachable = _reachable_from(graph.start, outgoing)
        for node_id in graph.nodes:
            if node_id not in reachable:
                raise CompileError(f"unreachable node {graph_id}/{node_id}")


def _validate_node_references(
    workflow: WorkflowDef,
    registry: CapabilityRegistry,
    graph_id: str,
    node_id: str,
    node: NodeDef,
) -> None:
    if node.retry is not None and node.retry not in workflow.retry:
        raise CompileError(f"unknown retry policy {node.retry} at {graph_id}/{node_id}")
    if node.timeout is not None and node.timeout not in workflow.timeout:
        raise CompileError(f"unknown timeout policy {node.timeout} at {graph_id}/{node_id}")
    if node.graph is not None and node.graph not in workflow.graphs:
        raise CompileError(f"unknown subgraph {node.graph} at {graph_id}/{node_id}")
    if node.capability is not None and node.capability not in registry.task_handlers:
        raise CompileError(f"unknown capability {node.capability} at {graph_id}/{node_id}")
    for validator_id in node.validators:
        if validator_id not in registry.commit_validators:
            raise CompileError(f"unknown validator {validator_id} at {graph_id}/{node_id}")


def _reachable_from(start: str, outgoing: dict[str, list[str]]) -> set[str]:
    reachable: set[str] = set()
    pending = [start]
    while pending:
        node_id = pending.pop()
        if node_id in reachable:
            continue
        reachable.add(node_id)
        pending.extend(reversed(outgoing[node_id]))
    return reachable


def _compile_graph(graph_id: str, graph: GraphDef) -> CompiledGraph:
    declaration_index = {node_id: index for index, node_id in enumerate(graph.nodes)}
    adjacency = {node_id: [] for node_id in graph.nodes}
    for edge in graph.edges:
        adjacency[edge.from_].append(edge.to)

    sccs = _strongly_connected_components(adjacency)
    ordered_sccs = _condensation_order(adjacency, sccs, declaration_index)
    component_position = {
        node_id: position for position, component in enumerate(ordered_sccs) for node_id in component
    }
    ranked = sorted(
        graph.nodes,
        key=lambda node_id: (component_position[node_id], declaration_index[node_id]),
    )
    topology_rank = {node_id: rank for rank, node_id in enumerate(ranked)}
    nodes = {
        node_id: CompiledNode(
            graph_id=graph_id,
            node_id=node_id,
            declaration_index=declaration_index[node_id],
            topology_rank=topology_rank[node_id],
            definition=_freeze_node(node),
            incoming=tuple(edge for edge in graph.edges if edge.to == node_id),
            outgoing=tuple(edge for edge in graph.edges if edge.from_ == node_id),
        )
        for node_id, node in graph.nodes.items()
    }
    return CompiledGraph(
        graph_id=graph_id,
        max_activations=graph.max_activations,
        start=graph.start,
        declaration_order=tuple(graph.nodes),
        nodes=nodes,
        edges=graph.edges,
        sccs=tuple(tuple(sorted(component, key=declaration_index.__getitem__)) for component in ordered_sccs),
    )


def _freeze_node(node: NodeDef) -> NodeDef:
    values = dict(node.__dict__)
    values["input"] = _freeze_json(node.input)
    return NodeDef.model_construct(_fields_set=node.model_fields_set, **values)


def _freeze_json(value: object) -> object:
    if isinstance(value, dict):
        return _FrozenDict({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def _strongly_connected_components(adjacency: dict[str, list[str]]) -> list[tuple[str, ...]]:
    next_index = 0
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    components: list[tuple[str, ...]] = []

    def visit(node_id: str) -> None:
        nonlocal next_index
        indices[node_id] = next_index
        lowlinks[node_id] = next_index
        next_index += 1
        stack.append(node_id)
        on_stack.add(node_id)

        for successor in adjacency[node_id]:
            if successor not in indices:
                visit(successor)
                lowlinks[node_id] = min(lowlinks[node_id], lowlinks[successor])
            elif successor in on_stack:
                lowlinks[node_id] = min(lowlinks[node_id], indices[successor])

        if lowlinks[node_id] != indices[node_id]:
            return
        component: list[str] = []
        while True:
            member = stack.pop()
            on_stack.remove(member)
            component.append(member)
            if member == node_id:
                break
        components.append(tuple(component))

    for node_id in adjacency:
        if node_id not in indices:
            visit(node_id)
    return components


def _condensation_order(
    adjacency: dict[str, list[str]],
    components: list[tuple[str, ...]],
    declaration_index: dict[str, int],
) -> list[tuple[str, ...]]:
    component_of = {
        node_id: component_index
        for component_index, component in enumerate(components)
        for node_id in component
    }
    outgoing: dict[int, set[int]] = {index: set() for index in range(len(components))}
    indegree = {index: 0 for index in range(len(components))}
    for source, targets in adjacency.items():
        source_component = component_of[source]
        for target in targets:
            target_component = component_of[target]
            if target_component == source_component or target_component in outgoing[source_component]:
                continue
            outgoing[source_component].add(target_component)
            indegree[target_component] += 1

    def component_key(component_index: int) -> int:
        return min(declaration_index[node_id] for node_id in components[component_index])

    ready = sorted(
        (component_index for component_index, degree in indegree.items() if degree == 0),
        key=component_key,
    )
    ordered: list[int] = []
    while ready:
        component_index = ready.pop(0)
        ordered.append(component_index)
        for target_component in sorted(outgoing[component_index], key=component_key):
            indegree[target_component] -= 1
            if indegree[target_component] == 0:
                ready.append(target_component)
        ready.sort(key=component_key)
    return [components[component_index] for component_index in ordered]


__all__ = [
    "CompileError",
    "CompiledGraph",
    "CompiledNode",
    "CompiledWorkflow",
    "compile_workflow",
]
