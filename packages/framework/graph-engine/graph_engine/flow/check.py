"""Compile-time checks. Domination is intentionally not decided here."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, get_args, get_origin

from pydantic import BaseModel

from graph_engine.flow.control import (
    RESERVED_CHANNELS,
    assert_model_path,
    build_schemas,
    literal_strings,
    model_annotations,
    optional_literal,
    select_literals,
    unwrap_annotated,
    unwrap_optional,
)
from graph_engine.flow.declare import (
    Flow,
    Gate,
    GateNode,
    Loop,
    Node,
    ParallelNode,
    StepNode,
    SubflowNode,
    declared_flow,
)
from graph_engine.flow.errors import FlowCheckError
from graph_engine.flow.protocol import view_op
from graph_engine.flow.sources import (
    Const,
    GateAction,
    GateField,
    InputSource,
    LedgerReceipt,
    LedgerRefs,
    LoopRound,
    LoopTarget,
    Target,
)


def check_flow(flow: Flow, stack: tuple[int, ...] = ()) -> None:
    """Reject a flow that breaks a rule the compiler can see before execution."""
    if id(flow) in stack:
        raise FlowCheckError(f"flow {flow.name} is mounted inside itself")
    if not flow.nodes:
        raise FlowCheckError(f"flow {flow.name} has no nodes")
    for name in flow.input.model_fields:
        if name in RESERVED_CHANNELS or name.startswith("__"):
            raise FlowCheckError(f"input {name} collides with a state channel")
    _check_public(flow)
    node_names = {node.name for node in flow.nodes}
    for loop in flow.loops:
        if not _exists(flow, node_names, loop.on_exhausted):
            raise FlowCheckError(f"loop {loop.name} on_exhausted {loop.on_exhausted} is unknown")
        if isinstance(loop.budget, str):
            assert_model_path(flow.input, loop.budget, what=f"loop {loop.name} budget")
    for node in flow.nodes:
        for target in _node_targets(node):
            _check_target(flow, node_names, node, target)
        if isinstance(node, StepNode):
            _check_step(flow, node)
        elif isinstance(node, GateNode):
            _check_gate(node.gate)
    links = _links(flow)
    _check_reachable(flow, node_names, links)
    writes = {node.name: _direct_writes(node, stack + (id(flow),)) for node in flow.nodes}
    for node in flow.nodes:
        if isinstance(node, StepNode):
            _check_bindings(flow, node, links, writes)
        elif isinstance(node, ParallelNode):
            _check_parallel(flow, node, links, writes, stack)
        elif isinstance(node, SubflowNode):
            _check_subflow(flow, node, links, writes, stack)
    _check_public_receipts(flow)
    _check_controls(flow)


@dataclass(frozen=True, slots=True)
class _SurfaceExport:
    path: str
    annotation: Any
    receipt: bool


def surface_controls(flow: Flow, stack: tuple[int, ...] = ()) -> dict[str, Any]:
    """Control-output names this flow shows its parent, with source types."""
    return {name: item.annotation for name, item in _surface_controls(flow, stack).items()}


def root_schemas(flow: Flow, *, outcome_field: str | None = None) -> tuple[type, type, type, frozenset[str]]:
    """Root input, state, and output schemas. Compile and product contracts share this."""
    return build_schemas(
        flow.name,
        flow.input,
        output_mode="root",
        outcome_field=outcome_field,
        public=flow.public is not None,
        controls=surface_controls(flow),
    )


def _surface_controls(flow: Flow, stack: tuple[int, ...] = ()) -> dict[str, _SurfaceExport]:
    """Control names are unique on this flow and every mounted child."""
    if id(flow) in stack:
        raise FlowCheckError(f"flow {flow.name} is mounted inside itself")
    found: dict[str, _SurfaceExport] = {}
    steps = {node.name: node for node in flow.nodes if isinstance(node, StepNode)}
    for item in flow.controls:
        step = steps.get(item.step)
        output_model = None if step is None else view_op(step.op).output_model
        for name, path in item.fields.items():
            if name in found:
                raise FlowCheckError(f"control {name} is declared twice")
            annotation = None if output_model is None else _path_annotation(output_model, path)
            found[name] = _SurfaceExport(path, annotation, False)
    nested = stack + (id(flow),)
    for node in flow.nodes:
        child = declared_flow(node.child) if isinstance(node, SubflowNode) else None
        if child is None:
            continue
        for name, item in _surface_controls(child, nested).items():
            if name in found:
                raise FlowCheckError(f"control {name} is declared twice")
            found[name] = item
    return found


def possible_writes(flow: Flow, stack: tuple[int, ...] = ()) -> set[str]:
    if id(flow) in stack:
        raise FlowCheckError(f"flow {flow.name} is mounted inside itself")
    nested = stack + (id(flow),)
    found: set[str] = set()
    for node in flow.nodes:
        found.update(_direct_writes(node, nested))
    return found


def _check_public(flow: Flow) -> None:
    if flow.public is None:
        return
    if "change_id" not in flow.input.model_fields:
        raise FlowCheckError("public flow input needs change_id")
    if set(flow.public) != set(flow.outcomes):
        raise FlowCheckError("public must cover every outcome")
    for value in flow.public.values():
        if value not in {"completed", "failed"}:
            raise FlowCheckError(f"public status {value!r} is not completed or failed")


def _check_step(flow: Flow, step: StepNode) -> None:
    view = view_op(step.op)
    if step.route_on is None:
        if step.routes is not None:
            raise FlowCheckError(f"{step.name} routes require route_on")
        if step.then is None:
            raise FlowCheckError(f"{step.name} needs then or route_on")
    else:
        if step.then is not None:
            raise FlowCheckError(f"{step.name} cannot set both then and route_on")
        if not step.routes:
            raise FlowCheckError(f"{step.name} routes must cover route_on")
        if step.route_on not in view.output_model.model_fields:
            raise FlowCheckError(f"{step.name} route_on field {step.route_on} is missing")
        annotation = model_annotations(view.output_model)[step.route_on]
        if optional_literal(annotation):
            raise FlowCheckError(f"{step.name} route_on {step.route_on} must not be an optional Literal")
        values = literal_strings(annotation)
        if values is None:
            raise FlowCheckError(f"{step.name} route_on {step.route_on} must be a Literal")
        if set(step.routes) != values:
            raise FlowCheckError(f"{step.name} routes for {step.route_on} must equal {sorted(values)}")
    _check_call_inputs(
        flow,
        step.name,
        step.loop,
        view.input_model,
        step.inputs,
        {item.field for item in view.bindings},
    )


def _check_gate(gate: Gate) -> None:
    if "action" not in gate.decision.model_fields:
        raise FlowCheckError(f"gate {gate.name} decision needs an action field")
    annotation = model_annotations(gate.decision)["action"]
    if optional_literal(annotation):
        raise FlowCheckError(f"gate {gate.name} action must not be an optional Literal")
    values = literal_strings(annotation)
    if values is None:
        raise FlowCheckError(f"gate {gate.name} action must be a Literal")
    if set(gate.routes) != values:
        raise FlowCheckError(f"gate {gate.name} routes must equal {sorted(values)}")


def _check_bindings(
    flow: Flow,
    step: StepNode,
    links: Mapping[str, tuple[str, ...]],
    writes: Mapping[str, set[str]],
) -> None:
    available = _available(flow, step.name, links, writes)
    for binding in view_op(step.op).bindings:
        if binding.ledger_key not in available:
            raise FlowCheckError(f"{step.name} ledger key {binding.ledger_key} has no upstream writer")


def _check_parallel(
    flow: Flow,
    node: ParallelNode,
    links: Mapping[str, tuple[str, ...]],
    writes: Mapping[str, set[str]],
    stack: tuple[int, ...],
) -> None:
    if node.select is not None:
        _check_select(flow, node)
    available = _available(flow, node.name, links, writes)
    groups: list[tuple[str, set[str]]] = []
    nested = stack + (id(flow),)
    for key, branch in node.branches.items():
        branch_flow = declared_flow(branch)
        if branch_flow is not None:
            check_flow(branch_flow, nested)
            outcomes = branch_flow.outcomes
            groups.append((key, possible_writes(branch_flow, nested)))
            for ledger_key in branch_flow.ledger_input_keys:
                if ledger_key not in available:
                    raise FlowCheckError(f"ledger key {ledger_key} is not available at {node.name}")
            _check_same_name_inputs(flow, f"{node.name}.{key}", branch_flow.input)
        else:
            view = view_op(branch)
            outcomes = ("succeeded", "failed")
            groups.append((key, set(view.write_keys())))
            for binding in view.bindings:
                if binding.ledger_key not in available:
                    raise FlowCheckError(f"ledger key {binding.ledger_key} is not available at {node.name}")
            _check_call_inputs(
                flow,
                f"{node.name}.{key}",
                None,
                view.input_model,
                {},
                {item.field for item in view.bindings},
            )
        if node.require not in outcomes:
            raise FlowCheckError(f"parallel {node.name} require {node.require} is not an outcome of {key}")
    for index, (left_key, left) in enumerate(groups):
        for right_key, right in groups[index + 1 :]:
            overlap = left & right
            if overlap:
                names = ", ".join(sorted(overlap))
                raise FlowCheckError(
                    f"parallel {node.name} branches {left_key} and {right_key} both write {names}"
                )


def _check_subflow(
    flow: Flow,
    node: SubflowNode,
    links: Mapping[str, tuple[str, ...]],
    writes: Mapping[str, set[str]],
    stack: tuple[int, ...],
) -> None:
    child = declared_flow(node.child)
    if child is not None:
        check_flow(child, stack + (id(flow),))
        expected = set(child.outcomes)
        _check_child_inputs(flow, node, child.input, links, _available(flow, node.name, links, writes))
        available = _available(flow, node.name, links, writes)
        for ledger_key in child.ledger_input_keys:
            if ledger_key not in available:
                raise FlowCheckError(f"ledger key {ledger_key} is not available at {node.name}")
    else:
        raise FlowCheckError("subflow child must be a bound flow")
    if set(node.routes) != expected:
        raise FlowCheckError(f"{node.name} routes must cover {sorted(expected)}")


def _check_controls(flow: Flow) -> None:
    steps = {node.name: node for node in flow.nodes if isinstance(node, StepNode)}
    input_fields = set(flow.input.model_fields)
    seen: set[str] = set()
    for item in flow.controls:
        step = steps.get(item.step)
        if step is None:
            raise FlowCheckError(f"control step {item.step} is unknown")
        output_model = view_op(step.op).output_model
        for name, path in item.fields.items():
            if name in seen:
                raise FlowCheckError(f"control {name} is declared twice")
            seen.add(name)
            _reject_channel_name(name, input_fields)
            assert_model_path(output_model, path, what=f"control {name}")
            if _path_annotation(output_model, path) is None:
                raise FlowCheckError(f"control {name} path {path} has no type")
    for name in surface_controls(flow):
        _reject_channel_name(name, input_fields)


def _check_public_receipts(flow: Flow) -> None:
    steps = {node.name: node for node in flow.nodes if isinstance(node, StepNode)}
    seen_public: set[str] = set()
    for step_name in flow.public_receipt_steps:
        if step_name not in steps:
            raise FlowCheckError(f"publish_receipt step {step_name} is unknown")
        if step_name in seen_public:
            raise FlowCheckError(f"publish_receipt step {step_name} is declared twice")
        seen_public.add(step_name)


def _reject_channel_name(name: str, input_fields: set[str]) -> None:
    if name in RESERVED_CHANNELS or name in input_fields or name.startswith("__"):
        raise FlowCheckError(f"control {name} collides with a state channel")


def _check_select(flow: Flow, node: ParallelNode) -> None:
    assert node.select is not None
    annotation = _path_annotation(flow.input, node.select)
    if annotation is None:
        raise FlowCheckError(f"parallel {node.name} select {node.select} is not an input field")
    inner = unwrap_optional(annotation)
    origin = get_origin(inner)
    if origin not in {list, tuple}:
        raise FlowCheckError(f"parallel {node.name} select must be a list or tuple")
    literals = select_literals(annotation)
    if literals is not None:
        unknown = sorted(value for value in literals if value not in node.branches)
        if unknown:
            raise FlowCheckError(f"parallel {node.name} select values {unknown} are not branches")
        return
    args = get_args(inner)
    element: Any = args[0] if args else None
    if origin is tuple and len(args) == 2 and args[1] is Ellipsis:
        element = args[0]
    if element is not str:
        raise FlowCheckError(f"parallel {node.name} select elements must be str")


def _check_same_name_inputs(flow: Flow, label: str, model: type[BaseModel]) -> None:
    parent = set(flow.input.model_fields)
    for name, field in model.model_fields.items():
        if name in parent or not field.is_required():
            continue
        raise FlowCheckError(f"{label} input {name} cannot be resolved")


def _check_child_inputs(
    flow: Flow,
    node: SubflowNode,
    model: type[BaseModel],
    links: Mapping[str, tuple[str, ...]],
    available_ledger: set[str],
) -> None:
    controls = _upstream_control_types(flow, node.name, links)
    _check_call_inputs(
        flow,
        node.name,
        node.loop,
        model,
        node.inputs,
        set(),
        control_types=controls,
    )
    for source in node.inputs.values():
        if isinstance(source, (LedgerRefs, LedgerReceipt)) and source.key not in available_ledger:
            raise FlowCheckError(f"ledger key {source.key} is not available at {node.name}")


def _upstream_control_types(
    flow: Flow,
    node_name: str,
    links: Mapping[str, tuple[str, ...]],
) -> dict[str, Any]:
    """Source types of control outputs written before ``node_name``."""
    found: dict[str, Any] = {}
    steps = {node.name: node for node in flow.nodes if isinstance(node, StepNode)}
    for item in flow.controls:
        if not _reaches(item.step, node_name, links):
            continue
        step = steps.get(item.step)
        output_model = None if step is None else view_op(step.op).output_model
        for name, path in item.fields.items():
            annotation = None if output_model is None else _path_annotation(output_model, path)
            found.setdefault(name, annotation)
    for other in flow.nodes:
        if not isinstance(other, SubflowNode) or not _reaches(other.name, node_name, links):
            continue
        child = declared_flow(other.child)
        if child is None:
            continue
        for name, annotation in surface_controls(child).items():
            found.setdefault(name, annotation)
    return found


def _check_call_inputs(
    flow: Flow,
    label: str,
    loop: Loop | None,
    model: type[BaseModel],
    inputs: Mapping[str, InputSource],
    bound_fields: set[str],
    *,
    control_types: Mapping[str, Any] | None = None,
) -> None:
    parent = set(flow.input.model_fields)
    controls = {} if control_types is None else control_types
    available = set(controls)
    annotations = model_annotations(model)
    for name, field in model.model_fields.items():
        if name in inputs:
            continue
        if name in controls:
            _require_control_type(label, name, annotations[name], controls[name])
        if name in parent or name in bound_fields or name in available or not field.is_required():
            continue
        raise FlowCheckError(f"{label} input {name} cannot be resolved")
    for name, source in inputs.items():
        if name not in model.model_fields:
            raise FlowCheckError(f"{label} input {name} is not on the model")
        if isinstance(source, str) and name in controls and "." in source:
            raise FlowCheckError(f"{label} reads control {name} by name")
        if isinstance(source, str) and source in controls:
            _require_control_type(label, name, annotations[name], controls[source])
        _check_source(flow, label, loop, source, controls)


def _require_control_type(label: str, name: str, receiver: Any, source: Any) -> None:
    if not _annotations_match(receiver, source):
        raise FlowCheckError(f"{label} input {name} type does not match its control output")


def _annotations_match(left: Any, right: Any) -> bool:
    left = unwrap_annotated(left)
    right = unwrap_annotated(right)
    if left == right:
        return True
    origin_left, origin_right = get_origin(left), get_origin(right)
    if origin_left is None or origin_left != origin_right:
        return False
    args_left, args_right = get_args(left), get_args(right)
    if len(args_left) != len(args_right):
        return False
    return all(_annotations_match(item, other) for item, other in zip(args_left, args_right, strict=True))


def _check_source(
    flow: Flow,
    label: str,
    loop: Loop | None,
    source: InputSource,
    control_types: Mapping[str, Any] | None = None,
) -> None:
    if isinstance(source, str):
        controls = {} if control_types is None else control_types
        head = source.split(".", 1)[0]
        if head in controls and "." in source:
            raise FlowCheckError(f"{label} reads control {head} by name")
        if source in controls:
            return
        assert_model_path(flow.input, source, what=f"{label} input")
        return
    if isinstance(source, Const):
        return
    if isinstance(source, LoopRound):
        if not isinstance(source.loop, Loop) or source.loop not in flow.loops:
            raise FlowCheckError(f"{label} loop.round is not a loop of {flow.name}")
        if loop is not source.loop:
            raise FlowCheckError(f"{label} loop.round is outside loop {source.loop.name}")
        return
    if isinstance(source, (LedgerRefs, LedgerReceipt)):
        return
    if isinstance(source, (GateAction, GateField)):
        gate = source.gate
        gate_name = next(
            (node.name for node in flow.nodes if isinstance(node, GateNode) and node.gate is gate),
            None,
        )
        if not isinstance(gate, Gate) or gate_name is None:
            raise FlowCheckError(f"{label} gate is not in {flow.name}")
        if isinstance(source, GateField) and source.name not in gate.decision.model_fields:
            raise FlowCheckError(f"gate {gate.name} has no field {source.name}")
        if not _reaches(gate_name, label.split(".", 1)[0], _links(flow)):
            raise FlowCheckError(f"gate {gate.name} is not upstream of {label}")
        return
    raise FlowCheckError(f"{label} input source is not supported")


def _check_target(flow: Flow, node_names: set[str], node: Node, target: Target) -> None:
    if isinstance(target, LoopTarget):
        loop = _declared_loop(flow, target.loop, node.name)
        if node.name not in loop.body:
            raise FlowCheckError(f"{node.name} loop.next is outside loop {loop.name}")
        if target.target not in loop.body:
            raise FlowCheckError(f"loop {loop.name} target {target.target} is outside the body")
        if not _exists(flow, node_names, loop.on_exhausted):
            raise FlowCheckError(f"loop {loop.name} on_exhausted {loop.on_exhausted} is unknown")
        return
    if not isinstance(target, str) or not _exists(flow, node_names, target):
        raise FlowCheckError(f"{node.name} target {target!r} is unknown")


def _check_reachable(flow: Flow, node_names: set[str], links: Mapping[str, tuple[str, ...]]) -> None:
    seen_nodes: set[str] = set()
    seen_outcomes: set[str] = set()
    pending = [flow.nodes[0].name]
    while pending:
        current = pending.pop()
        if current in seen_nodes or current in seen_outcomes:
            continue
        if current in node_names:
            seen_nodes.add(current)
            pending.extend(links[current])
            continue
        if current in flow.outcomes:
            seen_outcomes.add(current)
    missing = next((node.name for node in flow.nodes if node.name not in seen_nodes), None)
    if missing is not None:
        raise FlowCheckError(f"unreachable node {missing}")
    missing_outcome = next((name for name in flow.outcomes if name not in seen_outcomes), None)
    if missing_outcome is not None:
        raise FlowCheckError(f"unreachable outcome {missing_outcome}")


def _exists(flow: Flow, node_names: set[str], name: str) -> bool:
    return name in node_names or name in flow.outcomes


def _node_targets(node: Node) -> tuple[Target, ...]:
    if isinstance(node, StepNode):
        found: list[Target] = []
        if node.then is not None:
            found.append(node.then)
        if node.routes is not None:
            found.extend(node.routes.values())
        if isinstance(node.on_failure, str):
            found.append(node.on_failure)
        else:
            found.extend(node.on_failure.values())
        return tuple(found)
    if isinstance(node, GateNode):
        return tuple(node.gate.routes.values())
    if isinstance(node, ParallelNode):
        return (node.then, node.on_failure)
    return tuple(node.routes.values())


def _links(flow: Flow) -> dict[str, tuple[str, ...]]:
    return {
        node.name: tuple(name for target in _node_targets(node) for name in _target_names(target))
        for node in flow.nodes
    }


def _target_names(target: Target) -> tuple[str, ...]:
    if isinstance(target, LoopTarget):
        loop = target.loop
        if not isinstance(loop, Loop):
            raise FlowCheckError("loop.next does not name a loop")
        return (target.target, loop.on_exhausted)
    return (target,)


def _declared_loop(flow: Flow, loop: object, label: str) -> Loop:
    if not isinstance(loop, Loop) or loop not in flow.loops:
        raise FlowCheckError(f"{label} loop.next is not a loop of {flow.name}")
    return loop


def _reaches(start: str, goal: str, links: Mapping[str, tuple[str, ...]]) -> bool:
    pending = list(links.get(start, ()))
    seen: set[str] = set()
    while pending:
        current = pending.pop()
        if current == goal:
            return True
        if current in seen or current not in links:
            continue
        seen.add(current)
        pending.extend(links[current])
    return False


def _available(
    flow: Flow,
    node_name: str,
    links: Mapping[str, tuple[str, ...]],
    writes: Mapping[str, set[str]],
) -> set[str]:
    found = set(flow.ledger_input_keys)
    for writer, keys in writes.items():
        if _reaches(writer, node_name, links):
            found.update(keys)
    return found


def _direct_writes(node: Node, stack: tuple[int, ...]) -> set[str]:
    if isinstance(node, StepNode):
        return set(view_op(node.op).write_keys())
    if isinstance(node, ParallelNode):
        found: set[str] = set()
        for branch in node.branches.values():
            found.update(_branch_writes(branch, stack))
        return found
    if isinstance(node, SubflowNode):
        child = declared_flow(node.child)
        if child is not None:
            return possible_writes(child, stack)
    return set()


def _branch_writes(branch: object, stack: tuple[int, ...]) -> set[str]:
    child = declared_flow(branch)
    if child is not None:
        return possible_writes(child, stack)
    return set(view_op(branch).write_keys())


def _path_annotation(schema: type[BaseModel], path: str) -> Any | None:
    current: Any = schema
    parts = [part for part in path.split(".") if part]
    if not parts or len(parts) != path.count(".") + 1:
        return None
    for index, part in enumerate(parts):
        if not isinstance(current, type) or not issubclass(current, BaseModel):
            return None
        fields = model_annotations(current)
        if part not in fields:
            return None
        if index == len(parts) - 1:
            return fields[part]
        current = unwrap_optional(fields[part])
    return None


__all__ = ["check_flow", "possible_writes", "root_schemas", "surface_controls"]
