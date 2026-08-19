from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal, cast

from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.errors import GraphEngineError
from graph_engine.graph.compiler import CompiledGraph, CompiledNode, CompiledWorkflow
from graph_engine.graph.expressions import evaluate_expression
from graph_engine.plugin_api import TaskFailure
from graph_engine.runtime.events import (
    GraphCompleted,
    GraphFailed,
    GraphStarted,
    InvocationFinished,
    NodeActivated,
    NodeCompleted,
    NodeFailed,
    RuntimeEvent,
    TokenConsumed,
    TokenOffered,
)
from graph_engine.runtime.frozen_json import thaw_json
from graph_engine.runtime.models import (
    ActivationRecord,
    GraphInstanceRecord,
    InvocationProjection,
    PlanResult,
    PlannedTask,
    TokenRecord,
)


class PlanningError(GraphEngineError):
    """Raised when a projection cannot be planned against its compiled workflow."""


TerminalStatus = Literal["succeeded", "failed", "stopped"]
ExecutionMode = Literal["task", "structural", "unsupported"]
ConsumptionMode = Literal["one", "join", "all_available"]
StructuralOutputBuilder = Callable[
    ["_PlannerState", CompiledNode, ActivationRecord],
    JSONValue,
]


@dataclass(frozen=True, slots=True)
class _NodeBehavior:
    execution: ExecutionMode
    consumption: ConsumptionMode
    output_builder: StructuralOutputBuilder | None = None
    routes_completion: bool = False
    completes_graph: bool = False


def activation_id(
    graph_instance_id: str,
    node_id: str,
    generation: int,
    token_ids: tuple[str, ...],
) -> str:
    return canonical_digest(
        {
            "graph_instance_id": graph_instance_id,
            "node_id": node_id,
            "generation": generation,
            "token_ids": list(token_ids),
        }
    )


def task_id(activation: str) -> str:
    return canonical_digest({"activation_id": activation, "kind": "task"})


def _start_token_id(graph_instance_id: str, node_id: str) -> str:
    return canonical_digest(
        {
            "graph_instance_id": graph_instance_id,
            "kind": "graph_start",
            "target": node_id,
        }
    )


def _edge_token_id(
    graph_instance_id: str,
    source_activation_id: str,
    edge_index: int,
    source: str,
    target: str,
) -> str:
    return canonical_digest(
        {
            "edge_index": edge_index,
            "graph_instance_id": graph_instance_id,
            "kind": "edge",
            "source": source,
            "source_activation_id": source_activation_id,
            "target": target,
        }
    )


@dataclass(slots=True)
class _PlannerState:
    compiled: CompiledWorkflow
    projection: InvocationProjection
    graphs: dict[str, GraphInstanceRecord]
    tokens: dict[str, TokenRecord]
    activations: dict[str, ActivationRecord]
    activation_order: list[str]
    events: list[RuntimeEvent] = field(default_factory=list)
    tasks: list[PlannedTask] = field(default_factory=list)
    terminal: TerminalStatus | None = None
    reason: str | None = None

    @classmethod
    def from_projection(cls, compiled: CompiledWorkflow, projection: InvocationProjection) -> _PlannerState:
        return cls(
            compiled=compiled,
            projection=projection,
            graphs={item.graph_instance_id: item for item in projection.graph_instances},
            tokens={item.token_id: item for item in projection.offered_tokens},
            activations={item.activation_id: item for item in projection.activations},
            activation_order=[item.activation_id for item in projection.activations],
        )

    @property
    def invocation_id(self) -> str:
        assert self.projection.invocation_id is not None
        return self.projection.invocation_id

    def result(self) -> PlanResult:
        return PlanResult(
            tasks=() if self.terminal is not None else tuple(self.tasks),
            events=tuple(self.events),
            terminal=self.terminal,
            reason=self.reason,
        )


def plan_next(compiled: CompiledWorkflow, projection: InvocationProjection) -> PlanResult:
    """Purely plan the next authoritative event batch and runnable task attempts."""
    _validate_projection(compiled, projection)
    if projection.status != "running":
        terminal = cast(
            TerminalStatus | None,
            None if projection.status == "not_started" else projection.status,
        )
        return PlanResult(terminal=terminal, reason=projection.terminal_reason)

    state = _PlannerState.from_projection(compiled, projection)
    _ensure_root_started(state)
    for graph_record in sorted(state.graphs.values(), key=lambda item: item.graph_instance_id):
        if graph_record.status == "running":
            _ensure_start_token(state, graph_record)

    terminal_activations = _terminal_task_activations(state)
    if terminal_activations:
        if _has_running_attempt(state):
            return state.result()
        _finish_terminal_tasks(state, terminal_activations)
        return state.result()

    _settle_existing_activations(state)
    _finish_settled_graphs(state)
    if state.terminal is not None:
        return state.result()

    while True:
        candidate = _next_ready_activation(state)
        if candidate is None:
            break
        graph_instance_id, graph, node, token_ids = candidate
        if _activation_count(state, graph_instance_id) >= graph.max_activations:
            _fail_graph(state, graph_instance_id, f"max_activations_exceeded:{graph_instance_id}")
            break
        behavior = _behavior(node)
        if behavior.execution == "unsupported":
            raise PlanningError(f"planner does not support node kind {node.definition.kind!r}")
        activation = _activate(state, graph_instance_id, node, token_ids)
        if behavior.execution == "task":
            state.tasks.append(_planned_task(state, node, activation))
        else:
            _complete_structural(state, node, behavior, activation)
            _finish_settled_graphs(state)
            if state.terminal is not None:
                break

    return state.result()


def _validate_projection(compiled: CompiledWorkflow, projection: InvocationProjection) -> None:
    if projection.status == "not_started":
        raise PlanningError("invocation has not started")
    if projection.entrypoint not in compiled.entrypoints:
        raise PlanningError(f"unknown entrypoint {projection.entrypoint!r}")

    graphs = {item.graph_instance_id: item for item in projection.graph_instances}
    for graph in projection.graph_instances:
        if graph.graph_id not in compiled.graphs:
            raise PlanningError(f"unknown graph {graph.graph_id!r}")

    token_by_id = {token.token_id: token for token in projection.offered_tokens}
    for token in projection.offered_tokens:
        graph_record = graphs.get(token.graph_instance_id)
        if graph_record is None:
            raise PlanningError(f"token {token.token_id!r} references an unknown graph instance")
        graph = compiled.graphs[graph_record.graph_id]
        if token.target not in graph.nodes:
            raise PlanningError(f"token {token.token_id!r} targets unknown node {token.target!r}")
        if token.source is not None and token.source not in graph.nodes:
            raise PlanningError(f"token {token.token_id!r} has unknown source node {token.source!r}")

    for graph_record in projection.graph_instances:
        graph = compiled.graphs[graph_record.graph_id]
        source_less = tuple(
            token
            for token in projection.offered_tokens
            if token.graph_instance_id == graph_record.graph_instance_id and token.source is None
        )
        if not source_less:
            continue
        expected_id = _start_token_id(graph_record.graph_instance_id, graph.start)
        if (
            len(source_less) != 1
            or source_less[0].token_id != expected_id
            or source_less[0].target != graph.start
            or thaw_json(source_less[0].payload) != thaw_json(graph_record.input)
        ):
            raise PlanningError(
                f"graph instance {graph_record.graph_instance_id!r} has an invalid canonical start token"
            )

    generations: dict[tuple[str, str], int] = {}
    for activation in projection.activations:
        graph_record = graphs.get(activation.graph_instance_id)
        if graph_record is None:
            raise PlanningError(
                f"activation {activation.activation_id!r} references an unknown graph instance"
            )
        graph = compiled.graphs[graph_record.graph_id]
        node = graph.nodes.get(activation.node_id)
        if node is None:
            raise PlanningError(
                f"activation {activation.activation_id!r} references unknown node "
                f"{graph_record.graph_id}/{activation.node_id}"
            )
        key = (activation.graph_instance_id, activation.node_id)
        generation = generations.get(key, 0)
        expected = activation_id(
            activation.graph_instance_id,
            activation.node_id,
            generation,
            activation.token_ids,
        )
        if activation.activation_id != expected:
            raise PlanningError(f"activation {activation.activation_id!r} has a non-canonical id")
        generations[key] = generation + 1
        behavior = _behavior(node)
        if activation.attempts and behavior.execution != "task":
            raise PlanningError("only task nodes can contain attempt history")
        activation_tokens = tuple(token_by_id[token_id_] for token_id_ in activation.token_ids)
        if not _matches_consumption_contract(graph, node, behavior, activation_tokens):
            raise PlanningError(
                f"activation {activation.activation_id!r} violates the compiled consumption contract"
            )


def _matches_consumption_contract(
    graph: CompiledGraph,
    node: CompiledNode,
    behavior: _NodeBehavior,
    tokens: tuple[TokenRecord, ...],
) -> bool:
    if not tokens:
        return False
    predecessors = tuple(dict.fromkeys(edge.from_ for edge in node.incoming))
    for token in tokens:
        if token.source is None:
            if node.node_id != graph.start or token.token_id != _start_token_id(
                token.graph_instance_id, graph.start
            ):
                return False
        elif token.source not in predecessors:
            return False

    if behavior.consumption == "one":
        return len(tokens) == 1
    if behavior.consumption == "all_available":
        return tuple(token.token_id for token in tokens) == tuple(sorted(token.token_id for token in tokens))
    if node.definition.join == "any":
        return len(tokens) == 1 and tokens[0].source in predecessors
    return tuple(token.source for token in tokens) == predecessors


def _ensure_root_started(state: _PlannerState) -> GraphInstanceRecord:
    assert state.projection.entrypoint is not None
    graph_id = state.compiled.entrypoints[state.projection.entrypoint]
    roots = [item for item in state.graphs.values() if item.parent_graph_instance_id is None]
    if len(roots) > 1:
        raise PlanningError("projection contains multiple root graph instances")
    if roots:
        root = roots[0]
        if root.graph_id != graph_id:
            raise PlanningError("root graph does not match the invocation entrypoint")
        if root.status != "running":
            raise PlanningError("running invocation has a non-running root graph")
        return root
    if state.graphs:
        raise PlanningError("projection contains child graph instances without a root")

    root = GraphInstanceRecord(
        graph_instance_id=graph_id,
        graph_id=graph_id,
        parent_graph_instance_id=None,
        parent_node_id=None,
        input=None,
    )
    state.graphs[root.graph_instance_id] = root
    state.events.append(
        GraphStarted(
            graph_instance_id=root.graph_instance_id,
            graph_id=root.graph_id,
            input=root.input,
        )
    )
    return root


def _ensure_start_token(state: _PlannerState, graph_record: GraphInstanceRecord) -> None:
    graph = state.compiled.graphs[graph_record.graph_id]
    identifier = _start_token_id(graph_record.graph_instance_id, graph.start)
    if identifier in state.tokens:
        return
    token = TokenRecord(
        token_id=identifier,
        graph_instance_id=graph_record.graph_instance_id,
        source=None,
        target=graph.start,
        payload=graph_record.input,
    )
    state.tokens[token.token_id] = token
    state.events.append(
        TokenOffered(
            token_id=token.token_id,
            graph_instance_id=token.graph_instance_id,
            source=token.source,
            target=token.target,
            payload=token.payload,
        )
    )


def _terminal_task_activations(state: _PlannerState) -> tuple[ActivationRecord, ...]:
    terminal: list[ActivationRecord] = []
    for activation in state.activations.values():
        graph_record = state.graphs[activation.graph_instance_id]
        node = state.compiled.graphs[graph_record.graph_id].nodes[activation.node_id]
        if _behavior(node).execution != "task":
            continue
        if activation.status == "stopped":
            terminal.append(activation)
            continue
        if activation.status != "active" or not activation.attempts:
            continue
        latest = activation.attempts[-1]
        if latest.status != "failed":
            continue
        assert latest.failure is not None
        policy_name = node.definition.retry
        assert policy_name is not None
        policy = state.compiled.retry[policy_name]
        if latest.failure.kind not in policy.retry_on or latest.attempt >= policy.max_attempts:
            terminal.append(activation)
    return tuple(sorted(terminal, key=lambda item: item.activation_id))


def _has_running_attempt(state: _PlannerState) -> bool:
    return any(
        activation.attempts and activation.attempts[-1].status == "running"
        for activation in state.activations.values()
    )


def _finish_terminal_tasks(state: _PlannerState, terminal_activations: tuple[ActivationRecord, ...]) -> None:
    stopped = next(
        (activation for activation in terminal_activations if activation.status == "stopped"),
        None,
    )
    if stopped is not None:
        reason = stopped.attempts[-1].stop_reason
        assert reason is not None
        state.events.append(
            InvocationFinished(
                invocation_id=state.invocation_id,
                status="stopped",
                terminal_reason=reason,
            )
        )
        state.terminal = "stopped"
        state.reason = reason
        return

    graph_ids: set[str] = set()
    first_failure: tuple[ActivationRecord, TaskFailure] | None = None
    for activation in terminal_activations:
        failure = activation.attempts[-1].failure
        assert failure is not None
        state.events.append(NodeFailed(activation_id=activation.activation_id, failure=failure))
        state.activations[activation.activation_id] = activation.model_copy(
            update={"status": "failed", "failure": failure}
        )
        graph_ids.add(activation.graph_instance_id)
        if first_failure is None:
            first_failure = (activation, failure)
    assert first_failure is not None
    activation, failure = first_failure
    reason = f"task_failed:{activation.node_id}:{failure.kind}"
    for graph_instance_id in sorted(graph_ids):
        _mark_graph_failed(state, graph_instance_id, reason)
    state.events.append(
        InvocationFinished(
            invocation_id=state.invocation_id,
            status="failed",
            terminal_reason=reason,
        )
    )
    state.terminal = "failed"
    state.reason = reason


def _settle_existing_activations(state: _PlannerState) -> None:
    for activation_id_ in sorted(state.activation_order):
        activation = state.activations[activation_id_]
        graph_record = state.graphs[activation.graph_instance_id]
        graph = state.compiled.graphs[graph_record.graph_id]
        node = graph.nodes[activation.node_id]
        behavior = _behavior(node)
        if behavior.execution == "unsupported":
            raise PlanningError(f"planner does not support node kind {node.definition.kind!r}")
        if activation.status == "completed":
            if behavior.completes_graph:
                _finish_graph_if_settled(state, activation)
            elif behavior.routes_completion:
                _route_completion(state, graph, node, activation)
            if state.terminal is not None:
                return
            continue
        if activation.status != "active":
            continue
        if behavior.execution == "structural":
            _complete_structural(state, node, behavior, activation)
            if state.terminal is not None:
                return
            continue
        if not activation.attempts:
            state.tasks.append(_planned_task(state, node, activation))
            continue
        latest = activation.attempts[-1]
        if latest.status == "succeeded":
            completed = activation.model_copy(update={"status": "completed", "output": latest.output})
            state.events.append(NodeCompleted(activation_id=activation.activation_id, output=latest.output))
            state.activations[activation.activation_id] = completed
            if behavior.routes_completion:
                _route_completion(state, graph, node, completed)
        elif latest.status == "failed":
            assert latest.failure is not None
            state.tasks.append(_planned_task(state, node, activation))


def _next_ready_activation(
    state: _PlannerState,
) -> tuple[str, CompiledGraph, CompiledNode, tuple[str, ...]] | None:
    candidates: list[tuple[str, int, int, str, CompiledGraph, CompiledNode, tuple[str, ...]]] = []
    for graph_instance_id in sorted(state.graphs):
        graph_record = state.graphs[graph_instance_id]
        if graph_record.status != "running":
            continue
        graph = state.compiled.graphs[graph_record.graph_id]
        for node in sorted(
            graph.nodes.values(), key=lambda item: (item.topology_rank, item.declaration_index)
        ):
            token_ids = _ready_token_ids(state, graph_instance_id, node)
            if not token_ids:
                continue
            candidates.append(
                (
                    graph_instance_id,
                    node.topology_rank,
                    node.declaration_index,
                    token_ids[0],
                    graph,
                    node,
                    token_ids,
                )
            )
    if not candidates:
        return None
    graph_instance_id, _, _, _, graph, node, token_ids = min(candidates, key=lambda item: item[:4])
    return graph_instance_id, graph, node, token_ids


def _ready_token_ids(state: _PlannerState, graph_instance_id: str, node: CompiledNode) -> tuple[str, ...]:
    available = sorted(
        (
            token
            for token in state.tokens.values()
            if token.graph_instance_id == graph_instance_id
            and token.target == node.node_id
            and token.consumed_by is None
        ),
        key=lambda item: item.token_id,
    )
    if not available:
        return ()
    behavior = _behavior(node)
    if behavior.consumption == "all_available":
        return tuple(token.token_id for token in available)
    if behavior.consumption == "one" or node.definition.join == "any":
        return (available[0].token_id,)

    predecessors = tuple(dict.fromkeys(edge.from_ for edge in node.incoming))
    selected: list[str] = []
    for predecessor in predecessors:
        matching = [token for token in available if token.source == predecessor]
        if not matching:
            return ()
        selected.append(matching[0].token_id)
    return tuple(selected)


def _activation_count(state: _PlannerState, graph_instance_id: str) -> int:
    return sum(activation.graph_instance_id == graph_instance_id for activation in state.activations.values())


def _activate(
    state: _PlannerState,
    graph_instance_id: str,
    node: CompiledNode,
    token_ids: tuple[str, ...],
) -> ActivationRecord:
    generation = sum(
        activation.graph_instance_id == graph_instance_id and activation.node_id == node.node_id
        for activation in state.activations.values()
    )
    identifier = activation_id(graph_instance_id, node.node_id, generation, token_ids)
    for token_id_ in token_ids:
        token = state.tokens[token_id_]
        state.events.append(
            TokenConsumed(
                token_id=token_id_,
                graph_instance_id=graph_instance_id,
                node_id=node.node_id,
            )
        )
        state.tokens[token_id_] = token.model_copy(
            update={"consumed_by": node.node_id, "activation_id": identifier}
        )
    activation = ActivationRecord(
        activation_id=identifier,
        graph_instance_id=graph_instance_id,
        node_id=node.node_id,
        token_ids=token_ids,
    )
    state.activations[identifier] = activation
    state.activation_order.append(identifier)
    state.events.append(
        NodeActivated(
            activation_id=identifier,
            graph_instance_id=graph_instance_id,
            node_id=node.node_id,
            token_ids=token_ids,
        )
    )
    return activation


def _activation_input(state: _PlannerState, node: CompiledNode, activation: ActivationRecord) -> JSONValue:
    return cast(
        JSONValue,
        {
            "config": thaw_json(node.definition.input),
            "tokens": [thaw_json(state.tokens[token_id_].payload) for token_id_ in activation.token_ids],
        },
    )


def _planned_task(state: _PlannerState, node: CompiledNode, activation: ActivationRecord) -> PlannedTask:
    latest = activation.attempts[-1] if activation.attempts else None
    prior_failure = latest.failure if latest is not None and latest.status == "failed" else None
    attempt = len(activation.attempts) + 1
    capability_id = node.definition.capability
    retry_name = node.definition.retry
    timeout_name = node.definition.timeout
    assert capability_id is not None and retry_name is not None and timeout_name is not None
    policy = state.compiled.retry[retry_name]
    if prior_failure is not None and (
        prior_failure.kind not in policy.retry_on or attempt > policy.max_attempts
    ):
        raise PlanningError("terminal task failure was routed as a retry")
    return PlannedTask(
        invocation_id=state.invocation_id,
        task_id=task_id(activation.activation_id),
        activation_id=activation.activation_id,
        graph_instance_id=activation.graph_instance_id,
        node_id=node.node_id,
        capability_id=capability_id,
        attempt=attempt,
        input=_activation_input(state, node, activation),
        prior_failure=prior_failure,
        timeout_seconds=state.compiled.timeout[timeout_name].run_seconds,
        resources=node.definition.resources,
        validators=node.definition.validators,
    )


def _complete_structural(
    state: _PlannerState,
    node: CompiledNode,
    behavior: _NodeBehavior,
    activation: ActivationRecord,
) -> None:
    if behavior.output_builder is None:
        raise PlanningError(f"planner does not support structural node kind {node.definition.kind!r}")
    output = behavior.output_builder(state, node, activation)
    completed = activation.model_copy(update={"status": "completed", "output": output})
    state.activations[activation.activation_id] = completed
    state.events.append(NodeCompleted(activation_id=activation.activation_id, output=output))
    if behavior.completes_graph:
        _finish_graph_if_settled(state, completed)
    elif behavior.routes_completion:
        graph_record = state.graphs[activation.graph_instance_id]
        graph = state.compiled.graphs[graph_record.graph_id]
        _route_completion(state, graph, node, completed)


def _gate_output(state: _PlannerState, node: CompiledNode, activation: ActivationRecord) -> JSONValue:
    activation_input = _activation_input(state, node, activation)
    expression = node.definition.expression
    assert expression is not None
    scope = cast(dict[str, JSONValue], thaw_json(activation_input))
    return {"value": bool(evaluate_expression(expression, scope))}


def _join_output(state: _PlannerState, _node: CompiledNode, activation: ActivationRecord) -> JSONValue:
    return {"tokens": [thaw_json(state.tokens[token_id_].payload) for token_id_ in activation.token_ids]}


def _end_output(state: _PlannerState, _node: CompiledNode, activation: ActivationRecord) -> JSONValue:
    payloads = [thaw_json(state.tokens[token_id_].payload) for token_id_ in activation.token_ids]
    return payloads[0] if len(payloads) == 1 else {"tokens": payloads}


def _route_completion(
    state: _PlannerState,
    graph: CompiledGraph,
    node: CompiledNode,
    activation: ActivationRecord,
) -> None:
    activation_input = _activation_input(state, node, activation)
    scope = cast(
        dict[str, JSONValue],
        {
            "input": thaw_json(activation_input),
            "output": thaw_json(activation.output),
        },
    )
    outgoing_indices = [index for index, edge in enumerate(graph.edges) if edge.from_ == node.node_id]
    for edge_index in outgoing_indices:
        edge = graph.edges[edge_index]
        if edge.condition is not None and not bool(evaluate_expression(edge.condition, scope)):
            continue
        identifier = _edge_token_id(
            activation.graph_instance_id,
            activation.activation_id,
            edge_index,
            edge.from_,
            edge.to,
        )
        existing = state.tokens.get(identifier)
        if existing is not None:
            if (
                existing.graph_instance_id != activation.graph_instance_id
                or existing.source != edge.from_
                or existing.target != edge.to
                or thaw_json(existing.payload) != thaw_json(activation.output)
            ):
                raise PlanningError(f"derived token {identifier!r} disagrees with projection")
            continue
        token = TokenRecord(
            token_id=identifier,
            graph_instance_id=activation.graph_instance_id,
            source=edge.from_,
            target=edge.to,
            payload=activation.output,
        )
        state.tokens[identifier] = token
        state.events.append(
            TokenOffered(
                token_id=identifier,
                graph_instance_id=activation.graph_instance_id,
                source=edge.from_,
                target=edge.to,
                payload=activation.output,
            )
        )


def _finish_graph_if_settled(state: _PlannerState, end_activation: ActivationRecord) -> None:
    graph_instance_id = end_activation.graph_instance_id
    if any(
        token.graph_instance_id == graph_instance_id
        and (token.activation_id is None or state.activations[token.activation_id].status != "completed")
        for token in state.tokens.values()
    ):
        return
    if any(
        activation.graph_instance_id == graph_instance_id and activation.status != "completed"
        for activation in state.activations.values()
    ):
        return
    if any(
        graph.parent_graph_instance_id == graph_instance_id and graph.status != "completed"
        for graph in state.graphs.values()
    ):
        return
    record = state.graphs[graph_instance_id]
    if record.status != "running":
        return
    state.events.append(GraphCompleted(graph_instance_id=graph_instance_id, output=end_activation.output))
    state.graphs[graph_instance_id] = record.model_copy(
        update={"status": "completed", "output": end_activation.output}
    )
    if record.parent_graph_instance_id is None:
        state.events.append(InvocationFinished(invocation_id=state.invocation_id, status="succeeded"))
        state.terminal = "succeeded"


def _finish_settled_graphs(state: _PlannerState) -> None:
    while True:
        made_progress = False
        for graph_instance_id in sorted(state.graphs):
            if state.graphs[graph_instance_id].status != "running":
                continue
            completed_ends = sorted(
                (
                    activation
                    for activation in state.activations.values()
                    if activation.graph_instance_id == graph_instance_id
                    and activation.status == "completed"
                    and _behavior(
                        state.compiled.graphs[state.graphs[graph_instance_id].graph_id].nodes[
                            activation.node_id
                        ]
                    ).completes_graph
                ),
                key=lambda item: item.activation_id,
            )
            if completed_ends:
                _finish_graph_if_settled(state, completed_ends[0])
                if state.terminal is not None:
                    return
                made_progress = state.graphs[graph_instance_id].status == "completed" or made_progress
        if not made_progress:
            return


def _fail_graph(state: _PlannerState, graph_instance_id: str, reason: str) -> None:
    if _has_running_attempt(state):
        return
    _mark_graph_failed(state, graph_instance_id, reason)
    state.events.append(
        InvocationFinished(
            invocation_id=state.invocation_id,
            status="failed",
            terminal_reason=reason,
        )
    )
    state.terminal = "failed"
    state.reason = reason


def _mark_graph_failed(state: _PlannerState, graph_instance_id: str, reason: str) -> None:
    record = state.graphs[graph_instance_id]
    state.events.append(GraphFailed(graph_instance_id=graph_instance_id, reason=reason))
    state.graphs[graph_instance_id] = record.model_copy(update={"status": "failed", "failure_reason": reason})


_NODE_BEHAVIORS = {
    "task": _NodeBehavior(
        execution="task",
        consumption="one",
        routes_completion=True,
    ),
    "gate": _NodeBehavior(
        execution="structural",
        consumption="one",
        output_builder=_gate_output,
        routes_completion=True,
    ),
    "join": _NodeBehavior(
        execution="structural",
        consumption="join",
        output_builder=_join_output,
        routes_completion=True,
    ),
    "end": _NodeBehavior(
        execution="structural",
        consumption="all_available",
        output_builder=_end_output,
        completes_graph=True,
    ),
    "subgraph": _NodeBehavior(execution="unsupported", consumption="one"),
    "interrupt": _NodeBehavior(execution="unsupported", consumption="one"),
}


def _behavior(node: CompiledNode) -> _NodeBehavior:
    return _NODE_BEHAVIORS[node.definition.kind]


__all__ = [
    "PlanResult",
    "PlannedTask",
    "PlanningError",
    "activation_id",
    "plan_next",
    "task_id",
]
