from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import TYPE_CHECKING, Any, Protocol, Self

from langgraph.graph import StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.typing import StateT

import graph_engine.stategraph.routing as routing
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
            select=select,
            publish=publish,
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


__all__ = ["AttemptGraph", "HasContractId"]
