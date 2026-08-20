from __future__ import annotations

import math
from collections.abc import Mapping
from types import MappingProxyType
from typing import Annotated, Any, Literal, Protocol, Self, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    field_serializer,
    field_validator,
    model_validator,
)

from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.errors import GraphEngineError
from graph_engine.graph.schema import (
    EdgeDef,
    GraphDef,
    NodeDef,
    NodeKind,
    RetryPolicyDef,
    TimeoutPolicyDef,
    WorkflowDef,
    validate_node_shape,
)
from graph_engine.plugin_api import ResourceClaims


class _CapabilityRegistryView(Protocol):
    @property
    def task_handlers(self) -> Mapping[str, object]: ...

    @property
    def commit_validators(self) -> Mapping[str, object]: ...


class CompileError(GraphEngineError):
    """Raised when a structural workflow cannot be compiled."""


_COMPILED_CONFIG = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class _CompiledModel(BaseModel):
    model_config = _COMPILED_CONFIG


def _serialize_frozen_json_map(value: Mapping[str, object]) -> dict[str, Any]:
    return {key: _thaw_json(item) for key, item in value.items()}


FrozenJSONMap = Annotated[
    Mapping[str, object],
    PlainSerializer(_serialize_frozen_json_map, return_type=dict[str, Any]),
]


class CompiledNodeDefinition(_CompiledModel):
    kind: NodeKind
    capability: str | None = None
    graph: str | None = None
    join: Literal["all", "any"] | None = None
    expression: str | None = None
    reason: str | None = None
    actions: tuple[str, ...] = ()
    input: FrozenJSONMap = Field(default_factory=lambda: MappingProxyType({}))
    retry: str | None = None
    timeout: str | None = None
    resources: ResourceClaims = Field(default_factory=ResourceClaims)
    validators: tuple[str, ...] = ()

    @field_validator("input", mode="after")
    @classmethod
    def _freeze_input(cls, value: Mapping[str, object]) -> Mapping[str, object]:
        return _freeze_json_map(value)

    @model_validator(mode="after")
    def _validate_kind_shape(self) -> Self:
        validate_node_shape(self.kind, self.model_fields_set, self.__dict__)
        return self


class CompiledNode(_CompiledModel):
    graph_id: str
    node_id: str
    declaration_index: int
    topology_rank: int
    definition: CompiledNodeDefinition
    incoming: tuple[EdgeDef, ...]
    outgoing: tuple[EdgeDef, ...]

    @field_validator("definition", mode="before")
    @classmethod
    def _normalize_definition(cls, value: object) -> object:
        if isinstance(value, NodeDef):
            return value.model_dump(mode="python", by_alias=True, exclude_unset=True)
        return value

    @field_serializer("definition")
    def _serialize_definition(self, value: CompiledNodeDefinition) -> dict[str, Any]:
        return value.model_dump(mode="json", by_alias=True, exclude_unset=True)


CompiledNodeMap = Annotated[
    Mapping[str, CompiledNode],
    PlainSerializer(dict, return_type=dict[str, CompiledNode]),
]


class CompiledGraph(_CompiledModel):
    graph_id: str
    max_activations: int
    start: str
    declaration_order: tuple[str, ...]
    nodes: CompiledNodeMap
    edges: tuple[EdgeDef, ...]
    sccs: tuple[tuple[str, ...], ...]

    @field_validator("nodes", mode="after")
    @classmethod
    def _freeze_nodes(cls, value: Mapping[str, CompiledNode]) -> Mapping[str, CompiledNode]:
        return MappingProxyType(dict(value))


EntrypointMap = Annotated[
    Mapping[str, str],
    PlainSerializer(dict, return_type=dict[str, str]),
]
RetryMap = Annotated[
    Mapping[str, RetryPolicyDef],
    PlainSerializer(dict, return_type=dict[str, RetryPolicyDef]),
]
TimeoutMap = Annotated[
    Mapping[str, TimeoutPolicyDef],
    PlainSerializer(dict, return_type=dict[str, TimeoutPolicyDef]),
]
CompiledGraphMap = Annotated[
    Mapping[str, CompiledGraph],
    PlainSerializer(dict, return_type=dict[str, CompiledGraph]),
]


class CompiledWorkflow(_CompiledModel):
    name: str
    entrypoints: EntrypointMap
    retry: RetryMap
    timeout: TimeoutMap
    graphs: CompiledGraphMap
    digest: str

    @field_validator("entrypoints", "retry", "timeout", "graphs", mode="after")
    @classmethod
    def _freeze_mapping(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        return MappingProxyType(dict(value))


def compile_workflow(workflow: WorkflowDef, registry: _CapabilityRegistryView) -> CompiledWorkflow:
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


def _validate_workflow(workflow: WorkflowDef, registry: _CapabilityRegistryView) -> None:
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

    _validate_subgraph_dependency_dag(workflow)


def _validate_subgraph_dependency_dag(workflow: WorkflowDef) -> None:
    graph_order = {graph_id: index for index, graph_id in enumerate(workflow.graphs)}
    dependencies: dict[str, list[str]] = {graph_id: [] for graph_id in workflow.graphs}
    for graph_id, graph in workflow.graphs.items():
        seen: set[str] = set()
        for node in graph.nodes.values():
            target = node.graph
            if target is None or target in seen:
                continue
            dependencies[graph_id].append(target)
            seen.add(target)

    cyclic_components = [
        component
        for component in _strongly_connected_components(dependencies)
        if len(component) > 1 or any(graph_id in dependencies[graph_id] for graph_id in component)
    ]
    if not cyclic_components:
        return
    component = min(
        cyclic_components,
        key=lambda item: min(graph_order[graph_id] for graph_id in item),
    )
    ordered = sorted(component, key=graph_order.__getitem__)
    raise CompileError(f"recursive subgraph dependency cycle: {', '.join(ordered)}")


def _validate_node_references(
    workflow: WorkflowDef,
    registry: _CapabilityRegistryView,
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
    incoming_edges: dict[str, list[EdgeDef]] = {node_id: [] for node_id in graph.nodes}
    outgoing_edges: dict[str, list[EdgeDef]] = {node_id: [] for node_id in graph.nodes}
    for edge in graph.edges:
        adjacency[edge.from_].append(edge.to)
        incoming_edges[edge.to].append(edge)
        outgoing_edges[edge.from_].append(edge)

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
            definition=CompiledNodeDefinition.model_validate(
                node.model_dump(mode="python", by_alias=True, exclude_unset=True)
            ),
            incoming=tuple(incoming_edges[node_id]),
            outgoing=tuple(outgoing_edges[node_id]),
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


def _freeze_json_map(value: Mapping[str, object]) -> Mapping[str, object]:
    return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})


def _freeze_json(value: object) -> object:
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("compiled input numbers must be finite")
        return value
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("compiled input object keys must be strings")
        return _freeze_json_map(cast(Mapping[str, object], value))
    if isinstance(value, list | tuple):
        return tuple(_freeze_json(item) for item in value)
    raise TypeError(f"compiled input value is not JSON-compatible: {type(value).__name__}")


def _thaw_json(value: object) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _strongly_connected_components(adjacency: dict[str, list[str]]) -> list[tuple[str, ...]]:
    finish_order: list[str] = []
    visited: set[str] = set()
    for node_id in adjacency:
        if node_id in visited:
            continue
        visited.add(node_id)
        frames: list[tuple[str, int]] = [(node_id, 0)]
        while frames:
            current, next_successor = frames[-1]
            successors = adjacency[current]
            if next_successor < len(successors):
                successor = successors[next_successor]
                frames[-1] = (current, next_successor + 1)
                if successor not in visited:
                    visited.add(successor)
                    frames.append((successor, 0))
                continue
            finish_order.append(current)
            frames.pop()

    reverse_adjacency: dict[str, list[str]] = {node_id: [] for node_id in adjacency}
    for source, targets in adjacency.items():
        for target in targets:
            reverse_adjacency[target].append(source)

    components: list[tuple[str, ...]] = []
    assigned: set[str] = set()
    for node_id in reversed(finish_order):
        if node_id in assigned:
            continue
        assigned.add(node_id)
        component: list[str] = []
        pending = [node_id]
        while pending:
            current = pending.pop()
            component.append(current)
            for predecessor in reversed(reverse_adjacency[current]):
                if predecessor not in assigned:
                    assigned.add(predecessor)
                    pending.append(predecessor)
        components.append(tuple(component))
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
