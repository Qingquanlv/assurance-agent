"""Compile a flow into a StateGraph and hand it to the build context."""

from __future__ import annotations

import types
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Union, cast, get_args, get_origin

from langchain_core.runnables import RunnableConfig, RunnableLambda
from langgraph.errors import GraphInterrupt
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph as CompiledFlow
from langgraph.types import Command, Send, interrupt
from pydantic import BaseModel

from graph_engine.flow.activation import make_activation
from graph_engine.flow.check import check_flow, root_schemas, surface_controls
from graph_engine.flow.control import (
    RESERVED_CHANNELS,
    ROUTE_SENTINEL,
    attach_control,
    build_schemas,
    control_table,
    dig,
    merge_flow_control,
    read_round,
    unwrap_annotated,
    validate_input,
)
from graph_engine.flow.declare import (
    BoundFlow,
    Flow,
    GateNode,
    Loop,
    Node,
    ParallelNode,
    StepNode,
    SubflowNode,
    declared_flow,
)
from graph_engine.flow.errors import FlowCheckError
from graph_engine.flow.protocol import OpView, view_op
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
from graph_engine.stategraph.ledger import bind_input_slots, ledger_entry_receipt, ledger_refs
from graph_engine.stategraph.publish import bind_produced_artifacts, output_mapping, receipt_mapping

_ENTRY = "__flow_entry__"


@dataclass(frozen=True, slots=True)
class _Mount:
    semantic_prefix: str
    segments: tuple[str, ...]
    outer_loops: tuple[str, ...]
    interrupt_owner: str
    output: str
    path_segments: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class _LedgerKey:
    ledger_key: str


def compile_flow(
    flow: Flow,
    context: Any,
    mount: _Mount | None = None,
    *,
    outcome_field: str | None = None,
) -> CompiledFlow:
    """Check ``flow`` and compile it. Only a root call leaves ``mount`` empty.

    The entry node validates the projected input and writes back fields that
    validation filled or changed. An ``entry_update`` method can add more.
    """
    if _is_product_context(context):
        _check_product_root(flow)
    if mount is not None:
        outcome_field = None
    else:
        mount = _Mount(
            semantic_prefix="",
            segments=(),
            outer_loops=(),
            interrupt_owner=flow.name,
            output="root",
            path_segments={},
        )
    check_flow(flow)
    if mount.output == "root":
        _check_outcome_field(flow, outcome_field)
    return _Compiler(flow, context, mount, outcome_field=outcome_field).build()


class _Compiler:
    def __init__(
        self,
        flow: Flow,
        context: Any,
        mount: _Mount,
        *,
        outcome_field: str | None = None,
    ) -> None:
        self.flow = flow
        self.context = context
        self.mount = mount
        self.loop_keys = {id(loop): _control_key(mount.segments, loop.name) for loop in flow.loops}
        self.gate_keys = {
            id(node.gate): _control_key(mount.segments, mount.path_segments.get(node.name, node.name))
            for node in flow.nodes
            if isinstance(node, GateNode)
        }
        self.gate_arrival_keys = {
            node.name: self.gate_keys[id(node.gate)] for node in flow.nodes if isinstance(node, GateNode)
        }
        controls = surface_controls(flow)
        self.outcome_field = outcome_field if mount.output == "root" else None
        self.public = flow.public if mount.output == "root" and flow.public is not None else None
        if mount.output == "root":
            state_type, input_type, output_type, keys = root_schemas(flow, outcome_field=self.outcome_field)
        else:
            state_type, input_type, output_type, keys = build_schemas(
                flow.name,
                flow.input,
                output_mode=mount.output,
                outcome_field=self.outcome_field,
                public=self.public is not None,
                controls=controls,
            )
        self.state_keys = keys
        self.builder: StateGraph[Any] = StateGraph(
            state_type,
            input_schema=input_type,
            output_schema=output_type,
        )
        self._input_names = frozenset(flow.input.model_fields)

    def build(self) -> CompiledFlow:
        entry_name = self.flow.nodes[0].name
        self.builder.add_node(_ENTRY, self._entry)
        self.builder.add_edge(START, _ENTRY)
        self.builder.add_edge(_ENTRY, entry_name)
        for name in self.flow.outcomes:
            public_status = None if self.public is None else self.public[name]
            self.builder.add_node(name, _outcome_node(name, self.outcome_field, public_status))
            self.builder.add_edge(name, END)
        for node in self.flow.nodes:
            if isinstance(node, StepNode):
                self._add_step(node)
            elif isinstance(node, GateNode):
                self._add_gate(node)
            elif isinstance(node, ParallelNode):
                self._add_parallel(node)
            else:
                self._add_subflow(node)
        if self.mount.output == "root" and _is_product_context(self.context):
            return self.context.compile_root(self.builder)
        return self.context.compile_subgraph(self.builder)

    def _entry(self, state: object) -> dict[str, Any]:
        if not isinstance(state, Mapping):
            raise TypeError("flow state must be a mapping")
        validate_input(self.flow.input, state)
        update = _entry_update(self.flow.input, state)
        target = self.flow.nodes[0].name
        _dest, delta = self._steer(None, target, state)
        del _dest
        if delta:
            attach_control(update, loops=delta)
        arrival_key = self.gate_arrival_keys.get(target)
        if arrival_key is not None:
            attach_control(update, gate_failures={arrival_key: None})
        return update

    def _add_step(self, step: StepNode) -> None:
        view = view_op(step.op)
        path = self._path(step.name)
        attempt = self.context.attempt(
            view.contract_id,
            semantic_node_id=f"{view.namespace}.{path}",
            activation=make_activation(self.mount.segments, self._segment(step.name), self._enclosing(step)),
            select=self._select(step, view),
            publish=self._publish(step, view),
        )
        if not callable(attempt):
            raise FlowCheckError(f"{step.name} attempt node is not callable")

        invoke = cast(Callable[..., Awaitable[object]], attempt)
        semantic_id = f"{view.namespace}.{path}"

        async def step_node(state: object, runtime: object = None) -> Command[Any]:
            result = await invoke(state, runtime=runtime)
            if not isinstance(result, Mapping) or not isinstance(state, Mapping):
                raise TypeError(f"{step.name} attempt must return a mapping")
            raw = dict(result)
            route = raw.pop(ROUTE_SENTINEL, None)
            failure = raw.get("attempt_failure")
            update = {key: value for key, value in raw.items() if key in self.state_keys}
            noticed: Mapping[str, Any] | None = None
            if isinstance(failure, Mapping):
                target: Target = _match_failure(step.on_failure, failure)
                attach_control(update, failures={self._control(step.name): dict(failure)})
                noticed = failure
            elif step.route_on is None:
                if step.then is None:
                    raise FlowCheckError(f"{step.name} needs then or route_on")
                target = step.then
            else:
                if not isinstance(route, str) or step.routes is None or route not in step.routes:
                    raise ValueError(f"{step.name} route {route!r} is not declared")
                target = step.routes[route]
            return self._finish(step.name, target, state, update, failure=noticed, semantic_id=semantic_id)

        step_node.__name__ = f"step_{step.name.replace('-', '_')}"
        self.builder.add_node(step.name, step_node, destinations=self._destinations(_step_targets(step)))

    def _add_gate(self, node: GateNode) -> None:
        gate = node.gate
        actions = _action_values(gate.decision)
        interrupt_id = gate.interrupt_id or f"{self.mount.interrupt_owner}.{self._path(node.name)}"
        control_key = self.gate_keys[id(gate)]

        def gate_node(state: object) -> Command[Any]:
            if not isinstance(state, Mapping):
                raise TypeError("flow state must be a mapping")
            rounds = {key: read_round(control_table(state, "loops"), key) for key in self._enclosing(node)}
            ledger = state.get("artifact_ledger")
            payload: dict[str, Any] = {
                "reason": gate.name,
                "actions": list(actions),
                "interrupt_id": interrupt_id,
                "ordinal": 0,
                "rounds": rounds,
                "show": {
                    getattr(handle, "ledger_key"): ledger_refs(ledger, getattr(handle, "ledger_key"))
                    for handle in gate.show
                },
            }
            arrival = control_table(state, "gate_failures").get(control_key)
            if isinstance(arrival, Mapping):
                payload["failure"] = dict(arrival)
            raw = interrupt(payload)
            if isinstance(raw, str):
                decision = gate.decision.model_validate({"action": raw})
            elif isinstance(raw, Mapping):
                decision = gate.decision.model_validate(dict(raw))
            else:
                raise TypeError("gate resume must be an action string or a decision mapping")
            action = getattr(decision, "action", None)
            if not isinstance(action, str) or action not in gate.routes:
                raise ValueError(f"gate {gate.name} action {action!r} is not routed")
            update: dict[str, Any] = {}
            attach_control(update, gates={control_key: decision.model_dump(mode="json")})
            return self._finish(node.name, gate.routes[action], state, update)

        gate_node.__name__ = f"gate_{node.name.replace('-', '_')}"
        self.builder.add_node(node.name, gate_node, destinations=self._destinations(gate.routes.values()))

    def _add_parallel(self, node: ParallelNode) -> None:
        compiled = {key: self._compile_branch(node, key, branch) for key, branch in node.branches.items()}
        join_id = f"__flow_join__{node.name}"

        def fanout(state: object) -> Command[Any]:
            if not isinstance(state, Mapping):
                raise TypeError("flow state must be a mapping")
            chosen = self._chosen(node, state)
            skipped = {key: "skipped" for key in node.branches if key not in chosen}
            update: dict[str, Any] = {}
            attach_control(update, results=skipped)
            if not chosen:
                return Command(goto=join_id, update=update)
            sends = [Send(_branch_node_id(node.name, key), _plain_state(state)) for key in chosen]
            return Command(goto=sends, update=update)

        def join(state: object) -> Command[Any]:
            if not isinstance(state, Mapping):
                raise TypeError("flow state must be a mapping")
            chosen = self._chosen(node, state)
            results = control_table(state, "results")
            missing = [key for key in chosen if key not in results]
            if missing:
                raise ValueError(f"{node.name} is missing branch outcomes: {', '.join(missing)}")
            target: Target = (
                node.then if all(results[key] == node.require for key in chosen) else node.on_failure
            )
            return self._finish(node.name, target, state, {})

        fanout.__name__ = f"parallel_{node.name.replace('-', '_')}"
        branch_ids = tuple(_branch_node_id(node.name, key) for key in node.branches)
        self.builder.add_node(node.name, fanout, destinations=(*branch_ids, join_id))
        for key, (child, model) in compiled.items():
            branch_id = _branch_node_id(node.name, key)
            self.builder.add_node(branch_id, self._child_runner(key, child, model))
            self.builder.add_edge(branch_id, join_id)
        self.builder.add_node(
            join_id,
            join,
            defer=True,
            destinations=self._destinations((node.then, node.on_failure)),
        )

    def _add_subflow(self, node: SubflowNode) -> None:
        route_id = f"__flow_route__{node.name}"
        child_flow = declared_flow(node.child)
        if child_flow is not None:
            child_context = node.child.context if isinstance(node.child, BoundFlow) else self.context
            child = compile_flow(child_flow, child_context, self._subflow_mount(node))
            runner = self._child_runner(
                node.name,
                child,
                child_flow.input,
                inputs=node.inputs,
                controls=tuple(surface_controls(child_flow)),
            )
        else:
            raise FlowCheckError(f"{node.name} subflow child is not mountable")
        self.builder.add_node(node.name, runner)

        def route(state: object) -> Command[Any]:
            if not isinstance(state, Mapping):
                raise TypeError("flow state must be a mapping")
            outcome = control_table(state, "results").get(node.name)
            if not isinstance(outcome, str) or outcome not in node.routes:
                raise ValueError(f"{node.name} outcome {outcome!r} is not routed")
            return self._finish(node.name, node.routes[outcome], state, {})

        route.__name__ = f"route_{node.name.replace('-', '_')}"
        self.builder.add_node(route_id, route, destinations=self._destinations(node.routes.values()))
        self.builder.add_edge(node.name, route_id)

    def _child_runner(
        self,
        result_key: str,
        compiled: Any,
        model: type[BaseModel],
        *,
        inputs: Mapping[str, InputSource] | None = None,
        controls: tuple[str, ...] = (),
    ) -> Any:
        bound_inputs = {} if inputs is None else inputs
        accepts_none = _none_fields(model)

        def finish(payload: Mapping[str, Any], result: Mapping[str, Any]) -> dict[str, Any]:
            update: dict[str, Any] = {}
            delta = _ledger_delta(payload["artifact_ledger"], result.get("artifact_ledger"))
            if delta:
                update["artifact_ledger"] = delta
            for name in controls:
                if name in result:
                    update[name] = result[name]
            control = result.get("flow_control")
            failures = control.get("failures") if isinstance(control, Mapping) else None
            tables: dict[str, Mapping[str, Any]] = {"results": {result_key: _terminal(result)}}
            if isinstance(failures, Mapping) and failures:
                tables["failures"] = {result_key: dict(failures)}
            attach_control(update, **tables)
            receipts = control.get("public_receipts") if isinstance(control, Mapping) else None
            if isinstance(receipts, list) and receipts:
                update["flow_control"] = merge_flow_control(
                    update.get("flow_control"),
                    {"public_receipts": list(receipts)},
                )
            return update

        def run(state: object, config: RunnableConfig = None) -> dict[str, Any]:  # type: ignore[assignment]
            if not isinstance(state, Mapping):
                raise TypeError("flow state must be a mapping")
            payload = self._project(model, state, bound_inputs, accepts_none)
            return finish(payload, _unwrap_child(compiled.invoke(payload, config)))

        async def arun(state: object, config: RunnableConfig = None) -> dict[str, Any]:  # type: ignore[assignment]
            if not isinstance(state, Mapping):
                raise TypeError("flow state must be a mapping")
            payload = self._project(model, state, bound_inputs, accepts_none)
            return finish(payload, _unwrap_child(await compiled.ainvoke(payload, config)))

        run.__name__ = f"run_{result_key.replace('-', '_')}"
        arun.__name__ = run.__name__
        return RunnableLambda(run, afunc=arun, name=run.__name__)

    def _select(self, step: StepNode, view: OpView) -> object:
        explicit = step.inputs
        bindings = tuple(item for item in view.bindings if item.field not in explicit)
        names = self._input_names
        loop_keys = self.loop_keys
        gate_keys = self.gate_keys
        accepts_none = _none_fields(view.input_model)

        def select(state: object) -> dict[str, object]:
            if not isinstance(state, Mapping):
                raise TypeError("flow state must be a mapping")
            data: dict[str, object] = {}
            for name in view.input_model.model_fields:
                if name in explicit:
                    source = explicit[name]
                    if _absent_optional_field(self.flow.input, source, state):
                        continue
                    data[name] = _resolve(
                        source,
                        state,
                        loop_keys,
                        gate_keys,
                        absent_ok=accepts_none[name],
                    )
                elif name in names and name in state:
                    data[name] = state[name]
            return data

        # Explicit inputs stay as written. Slot overlay fills only the other bindings.
        return bind_input_slots(select, bindings)

    def _publish(self, step: StepNode, view: OpView) -> object:
        route_on = step.route_on
        controls = tuple(
            (field_name, path)
            for item in self.flow.controls
            if item.step == step.name
            for field_name, path in item.fields.items()
        )
        publish_public = step.name in self.flow.public_receipt_steps

        def publish(
            state: Mapping[str, object],
            output: object,
            receipt: object,
            *,
            committed: object = (),
        ) -> dict[str, object]:
            del state, committed
            payload = output_mapping(output)
            update: dict[str, object] = {}
            if route_on is not None:
                if route_on not in payload:
                    raise ValueError(f"{step.name} output is missing {route_on}")
                update[ROUTE_SENTINEL] = payload[route_on]
            for field_name, path in controls:
                try:
                    value = _control_value(payload, path)
                except KeyError as error:
                    raise ValueError(f"{step.name} output is missing {path}") from error
                if value is not _CONTROL_OMIT:
                    update[field_name] = value
            if publish_public:
                mapped = receipt_mapping(receipt)
                if mapped is None:
                    raise ValueError(f"{step.name} commit has no receipt")
                update["flow_control"] = {
                    "public_receipts": [
                        {
                            "receipt_id": mapped["receipt_id"],
                            "receipt_digest": mapped["receipt_digest"],
                        }
                    ]
                }
            return update

        if not view.writes:
            return publish
        return bind_produced_artifacts(publish, namespace=view.namespace, writes=view.writes)

    def _project(
        self,
        model: type[BaseModel],
        state: Mapping[str, Any],
        inputs: Mapping[str, InputSource],
        accepts_none: Mapping[str, bool],
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        for name in model.model_fields:
            if name in inputs:
                payload[name] = _resolve(
                    inputs[name],
                    state,
                    self.loop_keys,
                    self.gate_keys,
                    absent_ok=accepts_none[name],
                )
            elif name in state:
                payload[name] = state[name]
        ledger = state.get("artifact_ledger")
        payload["artifact_ledger"] = dict(ledger) if isinstance(ledger, Mapping) else {}
        control = state.get("flow_control")
        if isinstance(control, Mapping):
            child_control = {key: value for key, value in control.items() if key != "public_receipts"}
            payload["flow_control"] = merge_flow_control(None, child_control)
        else:
            payload["flow_control"] = {}
        return payload

    def _finish(
        self,
        source: str | None,
        target: Target,
        state: Mapping[str, Any],
        update: dict[str, Any],
        *,
        failure: Mapping[str, Any] | None = None,
        semantic_id: str | None = None,
    ) -> Command[Any]:
        dest, delta = self._steer(source, target, state)
        if delta:
            attach_control(update, loops=delta)
        arrival_key = self.gate_arrival_keys.get(dest)
        if arrival_key is not None:
            # Only a step's own on_failure may attach a failure. Every other entry clears it.
            notice = (
                _failure_notice(semantic_id, failure)
                if failure is not None and semantic_id is not None
                else None
            )
            attach_control(update, gate_failures={arrival_key: notice})
        return Command(goto=dest, update=update)

    def _steer(
        self,
        source: str | None,
        target: Target,
        state: Mapping[str, Any],
    ) -> tuple[str, dict[str, int]]:
        delta: dict[str, int] = {}
        if isinstance(target, LoopTarget):
            loop = target.loop
            if not isinstance(loop, Loop):
                raise FlowCheckError("loop.next does not name a loop")
            key = self.loop_keys[id(loop)]
            current = read_round(control_table(state, "loops"), key)
            if current < self._budget(loop, state):
                dest = target.target
                delta[key] = current + 1
            else:
                dest = loop.on_exhausted
        else:
            dest = target
        for loop in self.flow.loops:
            key = self.loop_keys[id(loop)]
            source_in = source is not None and source in loop.body
            if dest in loop.body and not source_in and key not in delta:
                delta[key] = 0
        return dest, delta

    def _budget(self, loop: Loop, state: Mapping[str, Any]) -> int:
        if isinstance(loop.budget, int) and not isinstance(loop.budget, bool):
            return loop.budget
        try:
            value = dig(state, str(loop.budget))
        except KeyError as error:
            raise ValueError(f"loop {loop.name} budget {loop.budget} is missing") from error
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"loop {loop.name} budget must be a non-negative integer")
        return value

    def _chosen(self, node: ParallelNode, state: Mapping[str, Any]) -> list[str]:
        if node.select is None:
            return list(node.branches)
        try:
            raw = dig(state, node.select) if "." in node.select else state.get(node.select)
        except KeyError as error:
            raise ValueError(f"{node.name} select {node.select} is missing") from error
        if raw is None or isinstance(raw, (str, bytes)) or not isinstance(raw, (list, tuple)):
            raise ValueError(f"{node.name} select must be a list of branch names")
        chosen: list[str] = []
        for item in raw:
            if not isinstance(item, str) or item not in node.branches:
                raise ValueError(f"{node.name} select value {item!r} is not a branch")
            if item in chosen:
                raise ValueError(f"{node.name} select value {item!r} is duplicated")
            chosen.append(item)
        return chosen

    def _compile_branch(self, node: ParallelNode, key: str, branch: object) -> tuple[Any, type[BaseModel]]:
        child_flow = declared_flow(branch)
        if child_flow is None:
            built = self._branch_flow(key, branch)
            return compile_flow(built, self.context, self._branch_mount(node, key, branch)), self.flow.input
        child_context = branch.context if isinstance(branch, BoundFlow) else self.context
        return (
            compile_flow(child_flow, child_context, self._branch_mount(node, key, branch)),
            child_flow.input,
        )

    def _branch_flow(self, key: str, branch: object) -> Flow:
        if declared_flow(branch) is not None:
            raise FlowCheckError(f"parallel branch {key} is already a flow")
        view = view_op(branch)
        seen: set[str] = set()
        handles: list[_LedgerKey] = []
        for binding in view.bindings:
            if binding.ledger_key in seen:
                continue
            seen.add(binding.ledger_key)
            handles.append(_LedgerKey(binding.ledger_key))
        flow = Flow(
            "op",
            input=self.flow.input,
            outcomes=("succeeded", "failed"),
            ledger_inputs=tuple(handles),
        )
        flow.step("run", branch, on_failure="failed", then="succeeded")
        return flow

    def _branch_mount(self, node: ParallelNode, key: str, branch: object) -> _Mount:
        flow_branch = declared_flow(branch) is not None
        prefix = _extend(self.mount.semantic_prefix, key) if flow_branch else self.mount.semantic_prefix
        return _Mount(
            semantic_prefix=prefix,
            segments=self.mount.segments + (f"p-{node.name}", f"b-{key}"),
            outer_loops=self._outer(node),
            interrupt_owner=self.mount.interrupt_owner,
            output="branch",
            path_segments={} if flow_branch else {"run": key},
        )

    def _subflow_mount(self, node: SubflowNode) -> _Mount:
        return _Mount(
            semantic_prefix=self.mount.semantic_prefix,
            segments=self.mount.segments + (f"s-{node.name}",),
            outer_loops=self._outer(node),
            interrupt_owner=self.mount.interrupt_owner,
            output="child",
            path_segments={},
        )

    def _outer(self, node: Node) -> tuple[str, ...]:
        if node.loop is None:
            return self.mount.outer_loops
        return self.mount.outer_loops + (self.loop_keys[id(node.loop)],)

    def _enclosing(self, node: Node) -> tuple[str, ...]:
        return self._outer(node)

    def _segment(self, node_name: str) -> str:
        return self.mount.path_segments.get(node_name, node_name)

    def _path(self, node_name: str) -> str:
        return _extend(self.mount.semantic_prefix, self._segment(node_name))

    def _control(self, node_name: str) -> str:
        return _control_key(self.mount.segments, self._segment(node_name))

    def _destinations(self, targets: Any) -> tuple[str, ...]:
        names: list[str] = []
        for target in targets:
            if isinstance(target, LoopTarget):
                loop = target.loop
                if not isinstance(loop, Loop):
                    raise FlowCheckError("loop.next does not name a loop")
                names.extend((target.target, loop.on_exhausted))
            else:
                names.append(target)
        return tuple(dict.fromkeys(names))


def _step_targets(step: StepNode) -> list[Target]:
    found: list[Target] = []
    if step.then is not None:
        found.append(step.then)
    if step.routes is not None:
        found.extend(step.routes.values())
    if isinstance(step.on_failure, str):
        found.append(step.on_failure)
    else:
        found.extend(step.on_failure.values())
    return found


def _action_values(model: type[BaseModel]) -> tuple[str, ...]:
    from typing import Literal, get_args, get_origin

    from graph_engine.flow.control import model_annotations, unwrap_annotated

    annotation = unwrap_annotated(model_annotations(model)["action"])
    if get_origin(annotation) is not Literal:
        raise FlowCheckError("gate action must be a Literal")
    return tuple(item for item in get_args(annotation) if isinstance(item, str))


def _match_failure(on_failure: str | Mapping[str, str], failure: Mapping[str, Any]) -> str:
    if isinstance(on_failure, str):
        return on_failure
    kind = failure.get("resolution_kind")
    if kind == "permanent":
        detail = failure.get("kind")
        specific = f"permanent:{detail}" if isinstance(detail, str) else ""
        if specific in on_failure:
            return on_failure[specific]
        if "permanent" in on_failure:
            return on_failure["permanent"]
    elif isinstance(kind, str) and kind in on_failure:
        return on_failure[kind]
    if "*" not in on_failure:
        raise ValueError("on_failure mapping is missing '*'")
    return on_failure["*"]


def _absent_optional_field(model: type[BaseModel], source: InputSource, state: Mapping[str, Any]) -> bool:
    """An optional flow field that entry did not write is left off the attempt.

    A dotted path, a parent key, or a required field still fails closed.
    """
    if not isinstance(source, str) or "." in source or source in state:
        return False
    field = model.model_fields.get(source)
    return field is not None and not field.is_required()


def _none_fields(model: type[BaseModel]) -> dict[str, bool]:
    """Whether each field may receive None for an empty ledger key.

    Computed when the step or child runner is compiled. Resolving an input
    reads the stored bool.
    """
    return {name: _field_accepts_none(model, name) for name in model.model_fields}


def _field_accepts_none(model: type[BaseModel], name: str) -> bool:
    """An empty ``many=False`` key may be None only when the field allows it."""
    field = model.model_fields.get(name)
    if field is None:
        return False
    inner = unwrap_annotated(field.annotation)
    origin = get_origin(inner)
    if origin is Union or origin is types.UnionType:
        return type(None) in get_args(inner)
    return False


def _resolve(
    source: InputSource,
    state: Mapping[str, Any],
    loop_keys: Mapping[int, str],
    gate_keys: Mapping[int, str],
    *,
    absent_ok: bool = False,
) -> object:
    if isinstance(source, str):
        try:
            return dig(state, source)
        except KeyError as error:
            raise ValueError(f"input {source} is missing") from error
    if isinstance(source, Const):
        return source.value
    if isinstance(source, LoopRound):
        key = loop_keys[id(source.loop)]
        return read_round(control_table(state, "loops"), key)
    if isinstance(source, (GateAction, GateField)):
        stored = control_table(state, "gates").get(gate_keys[id(source.gate)])
        if not isinstance(stored, Mapping):
            raise ValueError("gate decision is missing")
        field_name = "action" if isinstance(source, GateAction) else source.name
        if field_name not in stored:
            raise ValueError(f"gate field {field_name} is missing")
        return stored[field_name]
    if isinstance(source, LedgerRefs):
        refs = ledger_refs(state.get("artifact_ledger"), source.key)
        if source.many:
            return refs
        if len(refs) == 0 and absent_ok:
            return None
        if len(refs) != 1:
            raise ValueError(f"ledger key {source.key} must hold one ref")
        return refs[0]
    if isinstance(source, LedgerReceipt):
        receipt = ledger_entry_receipt(state.get("artifact_ledger"), source.key)
        if receipt is None and absent_ok:
            return None
        if receipt is None:
            raise ValueError(f"ledger key {source.key} must hold one receipt")
        return receipt
    raise TypeError("input source is not supported")


def _unwrap_child(result: object) -> Mapping[str, Any]:
    if not isinstance(result, Mapping):
        raise TypeError("compiled flow must return a mapping")
    interrupts = result.get("__interrupt__")
    if interrupts:
        if isinstance(interrupts, (tuple, list)):
            raise GraphInterrupt(tuple(interrupts))
        raise GraphInterrupt((interrupts,))
    return result


def _ledger_delta(before: object, after: object) -> dict[str, Any]:
    """Entries the child changed. Parallel children must not hand back copies of the parent ledger."""
    if not isinstance(after, Mapping):
        return {}
    previous = before if isinstance(before, Mapping) else {}
    return {str(key): value for key, value in after.items() if previous.get(key) != value}


def _terminal(result: Mapping[str, Any]) -> str:
    control = result.get("flow_control")
    if isinstance(control, Mapping) and isinstance(control.get("terminal"), str):
        return str(control["terminal"])
    outcome = result.get("flow_outcome")
    if isinstance(outcome, str):
        return outcome
    raise ValueError("child flow did not report an outcome")


def _outcome_node(name: str, outcome_field: str | None, public_status: str | None) -> Any:
    def outcome(state: object) -> dict[str, object]:
        update: dict[str, object] = {"flow_outcome": name, "flow_control": {"terminal": name}}
        if outcome_field is not None:
            update[outcome_field] = name
        if public_status is not None:
            if not isinstance(state, Mapping):
                raise TypeError("flow state must be a mapping")
            receipts = _public_receipts(state)
            update["output"] = {
                "change_id": state.get("change_id"),
                "status": public_status,
                "receipts": receipts,
            }
            update["status"] = public_status
            update["receipts"] = receipts
            update["terminal"] = {"status": public_status, "reason": name}
        return update

    outcome.__name__ = f"outcome_{name.replace('-', '_')}"
    return outcome


def _failure_notice(semantic_id: str, failure: Mapping[str, Any]) -> dict[str, Any]:
    notice: dict[str, Any] = {"node": semantic_id}
    if "resolution_kind" in failure:
        notice["resolution_kind"] = failure["resolution_kind"]
    if "kind" in failure:
        notice["kind"] = failure["kind"]
    if "message" in failure:
        notice["message"] = failure["message"]
    if "reason" in failure:
        notice["reason"] = failure["reason"]
    return notice


def _entry_update(model: type[BaseModel], state: Mapping[str, Any]) -> dict[str, Any]:
    """Write back fields validation filled or changed, then ``entry_update``."""
    from graph_engine.flow.control import project_input

    projected = project_input(model, state)
    validated = model.model_validate(projected)
    dumped = validated.model_dump(mode="json")
    update = {
        name: dumped[name]
        for name in validated.model_fields_set
        if name not in projected or _json_value(projected[name]) != dumped[name]
    }
    fill = getattr(validated, "entry_update", None)
    if not callable(fill):
        return update
    extra = fill()
    if not isinstance(extra, Mapping):
        raise TypeError("entry_update must return a mapping")
    update.update({str(key): value for key, value in extra.items()})
    return update


def _json_value(value: object) -> object:
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return dump(mode="json")
    return value


def _public_receipts(state: Mapping[str, Any]) -> list[dict[str, str]]:
    control = state.get("flow_control")
    raw = control.get("public_receipts") if isinstance(control, Mapping) else None
    by_id: dict[str, dict[str, str]] = {}
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, Mapping):
                continue
            receipt_id = item.get("receipt_id")
            digest = item.get("receipt_digest")
            if isinstance(receipt_id, str) and isinstance(digest, str):
                by_id[receipt_id] = {"receipt_id": receipt_id, "receipt_digest": digest}
    return [by_id[key] for key in sorted(by_id)]


def _is_product_context(context: object) -> bool:
    """Product contexts are marked explicitly so a subgraph compiler does not hide them."""
    return getattr(type(context), "product_context", False) is True


def _check_product_root(flow: Flow) -> None:
    for node in flow.nodes:
        if isinstance(node, SubflowNode):
            continue
        if isinstance(node, StepNode):
            raise FlowCheckError(f"product flow {flow.name} only allows subflow nodes; {node.name} is a step")
        raise FlowCheckError(
            f"product flow {flow.name} only allows subflow nodes; {node.name} is not a subflow"
        )


def _check_outcome_field(flow: Flow, outcome_field: str | None) -> None:
    if outcome_field is None:
        return
    if not isinstance(outcome_field, str) or not outcome_field or outcome_field.startswith("__"):
        raise FlowCheckError("outcome_field must be a public field name")
    if (
        outcome_field in RESERVED_CHANNELS
        or outcome_field in flow.input.model_fields
        or outcome_field in surface_controls(flow)
    ):
        raise FlowCheckError(f"outcome_field {outcome_field} collides with a state channel")


_CONTROL_OMIT = object()


def _control_value(payload: object, path: str) -> object:
    """Copy one control field. None before the leaf omits the write; a None leaf is kept."""
    current = payload
    for part in path.split("."):
        if current is None:
            return _CONTROL_OMIT
        if isinstance(current, Mapping) and part in current:
            current = current[part]
            continue
        if isinstance(current, BaseModel) and part in type(current).model_fields:
            current = getattr(current, part)
            continue
        raise KeyError(path)
    return current


def _plain_state(state: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): value for key, value in state.items()}


def _extend(prefix: str, segment: str) -> str:
    if not prefix:
        return segment
    return f"{prefix}.{segment}"


def _control_key(segments: tuple[str, ...], name: str) -> str:
    if not segments:
        return name
    return ".".join((*segments, name))


def _branch_node_id(name: str, key: str) -> str:
    return f"__flow_branch__{name}__{key}"


__all__ = ["compile_flow"]
