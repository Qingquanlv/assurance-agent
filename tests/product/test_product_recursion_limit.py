from __future__ import annotations

import ast
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypedDict, cast

import pytest
from langgraph.errors import GraphRecursionError
from langgraph.graph import START, StateGraph

from assurance_product.graphs.factory import (
    build_product_graphs,
    product_invoke_config,
)
from assurance_product.graphs.revisions import ENTRYPOINT_RECURSION_LIMITS
from assurance_product.models import PRODUCT_ENTRYPOINTS
from graph_engine.application.status import normalize_runtime_error
from graph_engine.boot.boot import EngineGraphBuildContext

from tests.product.test_product_stategraph_flow import _build_context
from tests.product.test_stategraph_entrypoints import _real_features


def test_product_root_tests_bind_declared_recursion_limits() -> None:
    root = Path(__file__).resolve().parent
    for path in (root / "test_product_stategraph_flow.py", root / "test_full_flow.py"):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr != "invoke":
                continue
            owner = node.func.value
            if isinstance(owner, ast.Subscript) and isinstance(owner.value, ast.Attribute):
                assert owner.value.attr != "entrypoints", (
                    f"{path.name} must invoke Product roots via invoke_product_root"
                )


def test_every_product_root_uses_its_exact_declared_recursion_limit() -> None:
    from assurance_product.graphs.factory import entrypoint_contracts

    graphs = build_product_graphs(context=_build_context(), features=_real_features())
    assert set(ENTRYPOINT_RECURSION_LIMITS) == set(PRODUCT_ENTRYPOINTS)
    contracts = entrypoint_contracts()
    for name in PRODUCT_ENTRYPOINTS:
        contract = graphs.contracts[name]
        assert contract.recursion_limit == ENTRYPOINT_RECURSION_LIMITS[name]
        assert contract.recursion_limit == contracts[name].recursion_limit
        config = product_invoke_config(name)
        assert config.get("recursion_limit") == ENTRYPOINT_RECURSION_LIMITS[name]


class _ToyState(TypedDict, total=False):
    change_id: str


def test_graph_recursion_error_normalizes_to_runtime_failure() -> None:
    builder: StateGraph[_ToyState] = StateGraph(_ToyState)

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
    limit = ENTRYPOINT_RECURSION_LIMITS["full"]
    with pytest.raises(GraphRecursionError) as raised:
        graph.invoke({"change_id": "CH-DEMO-001"}, config={"recursion_limit": limit})
    status = normalize_runtime_error(raised.value)
    assert status.status == "failed"
    assert status.reason == "graph_recursion_limit"


def test_business_budget_exhaustion_is_a_distinct_terminal() -> None:
    import asyncio

    from graph_engine.attempts.resolutions import PermanentTaskFailure

    from tests.product.test_full_flow import _case, _front, _invoke, _tail_until_inspect

    async def run() -> None:
        failed: dict[str, list[object]] = {}
        _front(failed)
        _case(failed)
        _tail_until_inspect(failed, ("satisfied", 0))
        failed["execution.execute"] = [PermanentTaskFailure(kind="invalid_output", message="execute failed")]
        result = await _invoke(failed)
        assert isinstance(result.result, dict)
        assert result.result["terminal"] == {"status": "failed", "reason": "not_achieved"}
        assert result.result.get("status") != "graph_recursion_limit"
        coverage: dict[str, list[object]] = {}
        _front(coverage)
        _case(coverage)
        _tail_until_inspect(coverage, ("coverage_insufficient", 0))
        stopped = await _invoke(
            coverage,
            budgets={
                "review_rounds": 2,
                "coverage_rounds": 0,
                "healing_rounds": 1,
                "execution_retries": 1,
            },
        )
        assert isinstance(stopped.result, dict)
        assert stopped.result["terminal"] == {"status": "failed", "reason": "not_achieved"}
        assert "coverage_needed_inbox" not in stopped.result

    asyncio.run(run())
