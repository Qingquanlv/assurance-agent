from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

from langgraph.graph import StateGraph

if TYPE_CHECKING:
    from graph_engine.boot.boot import CapabilityBuildContext


def add_attempt_node(
    builder: StateGraph[Any],
    context: CapabilityBuildContext,
    node_id: str,
    *,
    contract_id: str,
    activation: object,
    select: object,
    publish: object,
) -> None:
    builder.add_node(
        node_id,
        cast(
            Callable[..., Any],
            context.attempt(
                contract_id,
                semantic_node_id=node_id,
                activation=activation,
                select=select,
                publish=publish,
            ),
        ),
    )
