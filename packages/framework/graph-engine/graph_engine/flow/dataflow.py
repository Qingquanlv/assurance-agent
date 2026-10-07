"""Guaranteed ledger, control and gate sources across flow edges."""

from __future__ import annotations

from collections.abc import Iterator

from pydantic import BaseModel

from graph_engine.flow.control import model_annotations, unwrap_annotated, unwrap_optional

from graph_engine.flow.declare import (
    Flow,
    GateNode,
    Loop,
    Node,
    StepNode,
    SubflowNode,
    declared_flow,
)
from graph_engine.flow.errors import FlowCheckError
from graph_engine.flow.protocol import view_op
from graph_engine.flow.sources import LoopTarget, Target


def guaranteed(flow: Flow, channel: str = "ledger", stack: tuple[int, ...] = ()) -> dict[str, set[str]]:
    """Intersect all reachable incoming paths, including the first loop entry.

    Loop edges retain committed values, matching the last-write-wins runtime.
    The initial incoming path prevents a future back-edge from satisfying a
    first-entry dependency. Outcome entries summarize child guarantees.
    """
    if id(flow) in stack:
        raise FlowCheckError(f"flow {flow.name} is mounted inside itself")
    if not flow.nodes:
        raise FlowCheckError(f"flow {flow.name} has no nodes")
    nodes = {node.name: node for node in flow.nodes}
    incoming = {flow.nodes[0].name: {("ledger", key) for key in flow.ledger_input_keys}}
    pending = [flow.nodes[0].name]
    while pending:
        name = pending.pop()
        node = nodes.get(name)
        if node is None:
            continue
        for target, produced in _edges(flow, node, stack + (id(flow),)):
            available = incoming[name] | produced
            targets: list[tuple[str, set[tuple[str, str]]]]
            if isinstance(target, LoopTarget):
                loop = target.loop
                assert isinstance(loop, Loop)
                targets = [(target.target, available), (loop.on_exhausted, available)]
            else:
                targets = [(target, available)]
            for destination, keys in targets:
                previous = incoming.get(destination)
                merged = keys if previous is None else previous & keys
                if previous is None or merged != previous:
                    incoming[destination] = merged
                    pending.append(destination)
    return {name: {key for kind, key in values if kind == channel} for name, values in incoming.items()}


def _edges(flow: Flow, node: Node, stack: tuple[int, ...]) -> Iterator[tuple[Target, set[tuple[str, str]]]]:
    if isinstance(node, StepNode):
        writes = {("ledger", key) for key in view_op(node.op).write_keys()}
        writes.update(
            ("control", name)
            for item in flow.controls
            if item.step == node.name
            for name, path in item.fields.items()
            if _control_path_is_guaranteed(view_op(node.op).output_model, path)
        )
        for target in node.routes.values() if node.routes is not None else (node.then,):
            assert target is not None
            yield target, writes
        for target in (
            node.on_failure.values() if not isinstance(node.on_failure, str) else (node.on_failure,)
        ):
            yield target, set()
    elif isinstance(node, GateNode):
        for target in node.gate.routes.values():
            yield target, {("gate", node.name)}
    elif isinstance(node, SubflowNode):
        child = declared_flow(node.child)
        if child is None:
            raise FlowCheckError("subflow child must be a bound flow")
        outcomes = guaranteed(child, stack=stack)
        for outcome, target in node.routes.items():
            yield (
                target,
                {("ledger", key) for key in outcomes.get(outcome, set()) - set(child.ledger_input_keys)}
                | {("control", key) for key in guaranteed(child, "control", stack).get(outcome, set())},
            )
    else:
        writes: set[tuple[str, str]] = set()
        if node.select is None:
            for branch in node.branches.values():
                child = declared_flow(branch)
                if child is None:
                    keys = set(view_op(branch).write_keys()) if node.require == "succeeded" else set()
                else:
                    keys = guaranteed(child, stack=stack).get(node.require, set()) - set(
                        child.ledger_input_keys
                    )
                writes.update(("ledger", key) for key in keys)
        yield node.then, writes
        # A failed fan-out does not guarantee any particular branch committed.
        yield node.on_failure, set()


def _control_path_is_guaranteed(schema: type[BaseModel], path: str) -> bool:
    """None at an intermediate model omits the export; a None leaf is published."""
    current = schema
    for part in path.split(".")[:-1]:
        annotation = unwrap_annotated(model_annotations(current)[part])
        if unwrap_optional(annotation) != annotation:
            return False
        if not isinstance(annotation, type) or not issubclass(annotation, BaseModel):
            return False
        current = annotation
    return True
