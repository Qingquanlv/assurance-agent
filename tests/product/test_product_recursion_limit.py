from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

import pytest
from langgraph.errors import GraphRecursionError
from langgraph.graph import START, StateGraph

from assurance_product.graphs.factory import (
    build_product_graphs,
    invoke_product_root,
    product_invoke_config,
)
from assurance_product.graphs.revisions import ENTRYPOINT_CONTRACTS, ENTRYPOINT_RECURSION_LIMITS
from assurance_product.graphs.state import ProductState
from assurance_product.models import PRODUCT_ENTRYPOINTS
from graph_engine.application.status import normalize_runtime_error
from graph_engine.boot.boot import EngineGraphBuildContext

from tests.product.test_product_stategraph_flow import (
    _build_context,
    _flow_features,
    _product_graphs,
    _public_input,
)
from tests.product.test_stategraph_entrypoints import _real_features


def test_every_product_root_uses_its_exact_declared_recursion_limit() -> None:
    graphs = build_product_graphs(context=_build_context(), features=_real_features())
    assert set(ENTRYPOINT_RECURSION_LIMITS) == set(PRODUCT_ENTRYPOINTS)
    for name in PRODUCT_ENTRYPOINTS:
        contract = graphs.contracts[name]
        assert contract.recursion_limit == ENTRYPOINT_RECURSION_LIMITS[name]
        assert contract.recursion_limit == ENTRYPOINT_CONTRACTS[name].recursion_limit
        config = product_invoke_config(name)
        assert config.get("recursion_limit") == ENTRYPOINT_RECURSION_LIMITS[name]


def test_graph_recursion_error_normalizes_to_runtime_failure() -> None:
    builder: StateGraph[ProductState] = StateGraph(ProductState)

    def _loop(state: object) -> dict[str, object]:
        del state
        return {}

    builder.add_node("loop", cast(Callable[..., Any], _loop))
    builder.add_edge(START, "loop")
    builder.add_edge("loop", "loop")
    graph = EngineGraphBuildContext(
        contracts={},
        checkpointer=None,
        approved_source_roots=(),
    ).compile_root(builder)
    limit = ENTRYPOINT_RECURSION_LIMITS["execute"]
    with pytest.raises(GraphRecursionError) as raised:
        graph.invoke({"change_id": "CH-DEMO-001"}, config={"recursion_limit": limit})
    status = normalize_runtime_error(raised.value)
    assert status.status == "failed"
    assert status.reason == "graph_recursion_limit"


def test_business_budget_exhaustion_is_a_distinct_terminal() -> None:
    graphs = _product_graphs(
        _flow_features(
            execute={"status": "failed", "rounds_used": 1, "rounds_budget": 1},
            issue_analyze={
                "classification": "test",
                "fix_eligible": True,
                "rounds_used": 1,
                "rounds_budget": 1,
            },
        )
    )
    result = invoke_product_root(graphs, "execute", _public_input("execute"))
    assert result["terminal"] == "not-achieved"
    assert result.get("status") != "graph_recursion_limit"
    coverage = invoke_product_root(
        _product_graphs(
            _flow_features(assess={"coverage_state": "repair_required", "rounds_used": 2, "rounds_budget": 2})
        ),
        "execute",
        _public_input("execute"),
    )
    assert coverage["terminal"] == "not-achieved"
    inbox = coverage.get("coverage_needed_inbox") or {}
    assert isinstance(inbox, dict)
    assert inbox.get("arrivals", []) == []
