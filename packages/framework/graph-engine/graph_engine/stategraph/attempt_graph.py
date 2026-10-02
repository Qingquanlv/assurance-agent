from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import TYPE_CHECKING, Any, Protocol, Self

from langgraph.graph import StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.typing import StateT

import graph_engine.stategraph.routing as routing
from graph_engine.stategraph.ledger import InputBinding, NamedWrite, bind_input_slots
from graph_engine.stategraph.publish import bind_produced_artifacts
from graph_engine.stategraph.registration import add_attempt_node

if TYPE_CHECKING:
    from graph_engine.boot.boot import CapabilityBuildContext


class HasContractId(Protocol):
    @property
    def contract_id(self) -> str: ...


class AttemptGraph(StateGraph[StateT]):
    def __init__(
        self,
        state_schema: type[StateT],
        context: CapabilityBuildContext,
        *,
        namespace: str,
        activation: object | None = None,
        output_schema: type[Any] | None = None,
    ) -> None:
        if not namespace or namespace.startswith(".") or namespace.endswith("."):
            raise ValueError(
                f"namespace must be a non-empty string without leading or trailing dots, got {namespace!r}"
            )
        super().__init__(state_schema, output_schema=output_schema)
        self._context = context
        self._namespace = namespace
        self._activation = activation

    def add_attempt(
        self,
        node_id: str,
        contract: str | HasContractId,
        *,
        select: object,
        publish: object,
        activation: object | None = None,
        semantic_node_id: str | None = None,
    ) -> Self:
        resolved_select = select
        bindings = _input_bindings(contract)
        if bindings:
            resolved_select = bind_input_slots(select, bindings)
        resolved_publish = publish
        writes = _ledger_writes(contract)
        if writes:
            if not callable(publish):
                raise TypeError("publish must be callable")
            resolved_publish = bind_produced_artifacts(
                publish,
                namespace=_ledger_namespace(contract),
                writes=writes,
            )
        resolved_activation = self._activation if activation is None else activation
        if resolved_activation is None:
            raise ValueError(f"activation is required for attempt node {node_id!r}")
        resolved_semantic_node_id = (
            semantic_node_id if semantic_node_id is not None else f"{self._namespace}.{node_id}"
        )
        contract_id = contract if isinstance(contract, str) else contract.contract_id
        add_attempt_node(
            self,
            self._context,
            node_id,
            contract_id=contract_id,
            semantic_node_id=resolved_semantic_node_id,
            activation=resolved_activation,
            select=resolved_select,
            publish=resolved_publish,
        )
        return self

    def add_attempt_edge(self, source: str, target: str, *, on_failure: str) -> Self:
        routing.add_attempt_edge(self, source, target, on_failure=on_failure)
        return self

    def add_route(
        self,
        source: str,
        route: Callable[[Mapping[str, object]], str],
        *,
        targets: Iterable[str],
        on_failure: str | None = None,
    ) -> Self:
        routing.add_route(self, source, route, targets=targets, on_failure=on_failure)
        return self

    def compile_subgraph(self) -> CompiledStateGraph:
        return self._context.compile_subgraph(self)


def _ledger_namespace(contract: object) -> str:
    method = getattr(contract, "ledger_namespace", None)
    if not callable(method):
        raise TypeError("named writes require ledger_namespace()")
    namespace = method()
    if not isinstance(namespace, str) or not namespace:
        raise TypeError("ledger_namespace() must return a nonempty string")
    return namespace


def _ledger_writes(contract: object) -> tuple[NamedWrite, ...]:
    method = getattr(contract, "ledger_writes", None)
    if not callable(method):
        return ()
    raw = method()
    if not isinstance(raw, tuple):
        raise TypeError("ledger_writes() must return a tuple")
    writes = tuple(raw)
    if not all(isinstance(item, NamedWrite) for item in writes):
        raise TypeError("ledger_writes() must return NamedWrite values")
    return writes


def _input_bindings(contract: object) -> tuple[InputBinding, ...]:
    method = getattr(contract, "input_bindings", None)
    if not callable(method):
        return ()
    raw = method()
    if not isinstance(raw, tuple):
        raise TypeError("input_bindings() must return a tuple")
    bindings = tuple(raw)
    if not all(isinstance(item, InputBinding) for item in bindings):
        raise TypeError("input_bindings() must return InputBinding values")
    return bindings


__all__ = ["AttemptGraph", "HasContractId"]
