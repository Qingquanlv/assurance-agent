"""Declarative flow topology. Compilation lives in ``compile.py``."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pydantic import BaseModel

if TYPE_CHECKING:
    from langgraph.graph.state import CompiledStateGraph as CompiledFlow

from graph_engine.flow.errors import FlowCheckError
from graph_engine.flow.sources import (
    GateAction,
    GateField,
    InputSource,
    LoopRound,
    LoopTarget,
    Target,
)

_NAME = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_FIELD_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_FAILURE_KEYS = frozenset(
    {
        "rejected",
        "permanent",
        "permanent:invalid_output",
        "permanent:invalid_input",
        "committed_effect_failure",
        "*",
    }
)


def _check_name(value: object, *, what: str) -> str:
    if not isinstance(value, str) or _NAME.fullmatch(value) is None:
        raise FlowCheckError(f"{what} {value!r} is not a flow name")
    return value


def _check_outcome(value: object) -> str:
    if not isinstance(value, str) or not value or value.startswith("__") or "|" in value or ":" in value:
        raise FlowCheckError(f"outcome {value!r} is not a public name")
    return value


def _ledger_input_key(handle: object) -> str:
    key = getattr(handle, "ledger_key", None)
    if not isinstance(key, str) or not key:
        raise FlowCheckError("ledger_inputs entries must have a ledger_key")
    return key


@dataclass
class Loop:
    """One declared loop. ``next`` and ``round`` are the only ways to use it."""

    name: str
    budget: int | str
    on_exhausted: str
    body: list[str] = field(default_factory=list)

    def next(self, target: str) -> LoopTarget:
        """Advance this loop, then go to ``target`` when ``round < budget``."""
        _check_name(target, what="loop target")
        return LoopTarget(loop=self, target=target)

    @property
    def round(self) -> LoopRound:
        """Input source: the current round, starting at 0."""
        return LoopRound(loop=self)


@dataclass(frozen=True, slots=True)
class Gate:
    """A human decision compiled to ``interrupt``."""

    name: str
    decision: type[BaseModel]
    routes: Mapping[str, Target]
    show: tuple[object, ...]
    interrupt_id: str | None

    @property
    def action(self) -> GateAction:
        return GateAction(gate=self)

    def field(self, name: str) -> GateField:
        if name not in self.decision.model_fields:
            raise FlowCheckError(f"gate {self.name} has no field {name}")
        return GateField(gate=self, name=name)


@dataclass(frozen=True, slots=True)
class StepNode:
    name: str
    op: object
    then: Target | None
    route_on: str | None
    routes: Mapping[str, Target] | None
    on_failure: str | Mapping[str, str]
    inputs: Mapping[str, InputSource]
    loop: Loop | None


@dataclass(frozen=True, slots=True)
class GateNode:
    name: str
    gate: Gate
    loop: Loop | None


@dataclass(frozen=True, slots=True)
class ParallelNode:
    name: str
    branches: Mapping[str, object]
    select: str | None
    require: str
    then: Target
    on_failure: str
    loop: Loop | None


@dataclass(frozen=True, slots=True)
class BoundFlow:
    """A flow compiled later with the capability context it was bound to."""

    flow: Flow
    context: object

    def compile(self, *, outcome_field: str | None = None) -> CompiledFlow:
        from graph_engine.flow.compile import compile_flow

        return compile_flow(self.flow, self.context, outcome_field=outcome_field)


@dataclass(frozen=True, slots=True)
class SubflowNode:
    name: str
    child: Flow | BoundFlow
    routes: Mapping[str, Target]
    inputs: Mapping[str, InputSource]
    loop: Loop | None


Node = StepNode | GateNode | ParallelNode | SubflowNode


@dataclass(frozen=True, slots=True)
class Control:
    """Typed fields copied from one step output onto this flow's state.

    Parents read each name as a state field. The value is a field path on the
    step output. A parent that receives the field must use the same type.
    """

    step: str
    fields: Mapping[str, str]


class _LoopScope:
    def __init__(self, flow: Flow, loop: Loop) -> None:
        self._flow = flow
        self._loop = loop

    def __enter__(self) -> Loop:
        if self._flow._open_loop is not None:
            raise FlowCheckError("loops in one flow cannot nest")
        self._flow._open_loop = self._loop
        return self._loop

    def __exit__(self, *_exc: object) -> bool:
        self._flow._open_loop = None
        return False


class Flow:
    """One declared graph. The first node is the entry.

    At entry, compilation validates the projected input and writes back fields the
    validator filled or changed. ``entry_update`` may return more of those fields.
    """

    def __init__(
        self,
        name: str,
        *,
        input: type[BaseModel],
        outcomes: tuple[str, ...],
        public: Mapping[str, str] | None = None,
        ledger_inputs: tuple[object, ...] = (),
    ) -> None:
        self.name = _check_name(name, what="flow")
        if not isinstance(input, type) or not issubclass(input, BaseModel):
            raise FlowCheckError("flow input must be a Pydantic model")
        if not isinstance(outcomes, tuple) or not outcomes:
            raise FlowCheckError("flow outcomes must be a nonempty tuple")
        checked: list[str] = []
        for outcome in outcomes:
            checked.append(_check_outcome(outcome))
        if len(checked) != len(set(checked)):
            raise FlowCheckError("flow outcomes must be unique")
        self.input = input
        self.outcomes = tuple(checked)
        self.public = None if public is None else dict(public)
        keys: list[str] = []
        for handle in ledger_inputs:
            key = _ledger_input_key(handle)
            if key in keys:
                raise FlowCheckError(f"ledger input {key} is declared twice")
            keys.append(key)
        self.ledger_input_keys = tuple(keys)
        self.nodes: list[Node] = []
        self.loops: list[Loop] = []
        self.controls: list[Control] = []
        self.public_receipt_steps: list[str] = []
        self._open_loop: Loop | None = None

    def step(
        self,
        name: str,
        op: object,
        *,
        on_failure: str | Mapping[str, str],
        then: Target | None = None,
        route_on: str | None = None,
        routes: Mapping[str, Target] | None = None,
        inputs: Mapping[str, InputSource] | None = None,
    ) -> str:
        """Declare one attempt. Returns the step name."""
        self._add(
            StepNode(
                name=self._claim(name),
                op=op,
                then=then,
                route_on=route_on,
                routes=None if routes is None else dict(routes),
                on_failure=_failure(on_failure),
                inputs={} if inputs is None else dict(inputs),
                loop=self._open_loop,
            )
        )
        return name

    def loop(self, name: str, *, budget: int | str, on_exhausted: str) -> _LoopScope:
        """Open a loop body. Nested loops in the same flow are rejected."""
        if self._open_loop is not None:
            raise FlowCheckError("loops in one flow cannot nest")
        checked = self._claim(name)
        if isinstance(budget, bool) or not isinstance(budget, (int, str)):
            raise FlowCheckError(f"loop {checked} budget must be an integer or an input path")
        if isinstance(budget, int) and budget < 0:
            raise FlowCheckError(f"loop {checked} budget must be non-negative")
        if isinstance(budget, str) and not budget:
            raise FlowCheckError(f"loop {checked} budget path is empty")
        if not isinstance(on_exhausted, str) or not on_exhausted:
            raise FlowCheckError(f"loop {checked} on_exhausted must be a name")
        opened = Loop(name=checked, budget=budget, on_exhausted=on_exhausted)
        self.loops.append(opened)
        return _LoopScope(self, opened)

    def gate(
        self,
        name: str,
        *,
        decision: type[BaseModel],
        routes: Mapping[str, Target],
        show: tuple[object, ...] = (),
        interrupt_id: str | None = None,
    ) -> Gate:
        """Declare a human gate and return its input sources."""
        if not isinstance(decision, type) or not issubclass(decision, BaseModel):
            raise FlowCheckError("gate decision must be a Pydantic model")
        if interrupt_id is not None and (not isinstance(interrupt_id, str) or not interrupt_id):
            raise FlowCheckError("interrupt_id must be a nonempty string")
        for handle in show:
            key = getattr(handle, "ledger_key", None)
            if not isinstance(key, str) or not key:
                raise FlowCheckError(f"gate {name} show entries must have a ledger_key")
        opened = Gate(
            name=self._claim(name),
            decision=decision,
            routes=dict(routes),
            show=tuple(show),
            interrupt_id=interrupt_id,
        )
        self._add(GateNode(name=opened.name, gate=opened, loop=self._open_loop))
        return opened

    def parallel(
        self,
        name: str,
        *,
        branches: Mapping[str, object],
        select: str | None,
        require: str,
        then: Target,
        on_failure: str,
    ) -> None:
        """Fan out to fixed branches and join when every selected branch finishes."""
        if not branches:
            raise FlowCheckError("parallel branches must be nonempty")
        checked: dict[str, object] = {}
        for key, branch in branches.items():
            checked[_check_name(key, what="branch")] = branch
        if select is not None and (not isinstance(select, str) or not select):
            raise FlowCheckError("parallel select must be an input field")
        if not isinstance(require, str) or not require:
            raise FlowCheckError("parallel require must be an outcome name")
        if not isinstance(on_failure, str) or not on_failure:
            raise FlowCheckError("parallel on_failure must be a name")
        self._add(
            ParallelNode(
                name=self._claim(name),
                branches=checked,
                select=select,
                require=require,
                then=then,
                on_failure=on_failure,
                loop=self._open_loop,
            )
        )

    def subflow(
        self,
        name: str,
        child: Flow | BoundFlow,
        *,
        routes: Mapping[str, Target],
        inputs: Mapping[str, InputSource] | None = None,
    ) -> None:
        """Mount a child flow or a flow bound to its capability context."""
        if not isinstance(child, (Flow, BoundFlow)):
            raise FlowCheckError("subflow child must be a Flow or a bound flow")
        self._add(
            SubflowNode(
                name=self._claim(name),
                child=child,
                routes=dict(routes),
                inputs={} if inputs is None else dict(inputs),
                loop=self._open_loop,
            )
        )

    def control(self, step: str, **fields: str) -> None:
        """Publish typed output fields as ordinary state fields.

        Each keyword is the state field. Each value is a path on that step's
        output. A parent reads the state field by name, and its receiving
        field must have the source field's type.
        """
        if not isinstance(step, str) or not step:
            raise FlowCheckError("control step must be a name")
        if not fields:
            raise FlowCheckError("control needs at least one field")
        for field_name, path in fields.items():
            if not isinstance(field_name, str) or _FIELD_NAME.fullmatch(field_name) is None:
                raise FlowCheckError(f"control field {field_name!r} is not a name")
            if not isinstance(path, str) or not path:
                raise FlowCheckError(f"control {field_name} path must be nonempty")
        self.controls.append(Control(step=step, fields=dict(fields)))

    def publish_receipt(self, step: str) -> None:
        """Mark one step's commit receipt as public.

        The root flow collects these through nested subflows into ``receipts``.
        """
        if not isinstance(step, str) or not step:
            raise FlowCheckError("publish_receipt step must be a name")
        self.public_receipt_steps.append(step)

    def bind(self, context: object) -> BoundFlow:
        """Bind this flow to the capability context that will compile it."""
        if not _context_provides(context, "attempt") or not _context_provides(context, "compile_subgraph"):
            raise FlowCheckError("bind context must provide attempt and compile_subgraph")
        return BoundFlow(self, context)

    def compile(self, context: object, *, outcome_field: str | None = None) -> CompiledFlow:
        """Compile this root flow. ``outcome_field`` is ignored on mounted children."""
        from graph_engine.flow.compile import compile_flow

        return compile_flow(self, context, outcome_field=outcome_field)

    def _claim(self, name: str) -> str:
        checked = _check_name(name, what="node")
        if checked in self.outcomes:
            raise FlowCheckError(f"name {checked} is already an outcome")
        if any(node.name == checked for node in self.nodes):
            raise FlowCheckError(f"name {checked} is already used")
        if any(loop.name == checked for loop in self.loops):
            raise FlowCheckError(f"name {checked} is already used")
        return checked

    def _add(self, node: Node) -> None:
        if node.loop is not None:
            node.loop.body.append(node.name)
        self.nodes.append(node)


def _context_provides(context: object, name: str) -> bool:
    """True when ``name`` is a real callable, without triggering a raising ``__getattr__``."""
    for klass in type(context).__mro__:
        candidate = klass.__dict__.get(name)
        if candidate is not None:
            return callable(candidate)
    instance = getattr(context, "__dict__", None)
    if isinstance(instance, dict) and name in instance:
        return callable(instance[name])
    return False


def declared_flow(child: object) -> Flow | None:
    """Return the flow inside a ``Flow`` or ``BoundFlow`` child."""
    if isinstance(child, BoundFlow):
        return child.flow
    if isinstance(child, Flow):
        return child
    return None


def _failure(value: object) -> str | Mapping[str, str]:
    if isinstance(value, str) and value:
        return value
    if isinstance(value, Mapping):
        if "*" not in value:
            raise FlowCheckError("on_failure mapping must include '*'")
        checked: dict[str, str] = {}
        for key, target in value.items():
            if key not in _FAILURE_KEYS:
                raise FlowCheckError(f"on_failure key {key!r} is not a failure kind")
            if not isinstance(target, str) or not target:
                raise FlowCheckError(f"on_failure {key} must name a target")
            checked[str(key)] = target
        return checked
    raise FlowCheckError("on_failure must be a target or a failure-kind mapping")


__all__ = [
    "BoundFlow",
    "Flow",
    "Gate",
    "GateNode",
    "Loop",
    "Node",
    "ParallelNode",
    "StepNode",
    "SubflowNode",
    "declared_flow",
]
