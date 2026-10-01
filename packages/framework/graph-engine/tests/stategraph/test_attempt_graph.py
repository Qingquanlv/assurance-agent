from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, TypedDict

import pytest
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from graph_engine.stategraph import AttemptGraph


class _State(TypedDict, total=False):
    value: str


class _Context:
    owner_id = "test.owner"

    def __init__(self) -> None:
        self.builders: list[object] = []

    def attempt(
        self,
        contract_id: str,
        *,
        semantic_node_id: str,
        activation: object,
        select: object,
        publish: object,
    ) -> object:
        del contract_id, semantic_node_id, activation, select, publish
        raise AssertionError("add_attempt_node should be patched")

    def compile_subgraph(self, builder: StateGraph[Any]) -> CompiledStateGraph:
        self.builders.append(builder)
        return builder.compile()


class _Contract:
    def __init__(self, contract_id: str) -> None:
        self.contract_id = contract_id


def _select(state: _State) -> _State:
    return state


def _publish(state: _State, output: object, receipt: object) -> dict[str, object]:
    del state, output, receipt
    return {}


def _record(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    recorded: list[dict[str, object]] = []

    def spy(
        builder: object,
        context: object,
        node_id: str,
        *,
        contract_id: str,
        activation: object,
        select: object,
        publish: object,
        semantic_node_id: str | None = None,
    ) -> None:
        recorded.append(
            {
                "builder": builder,
                "context": context,
                "node_id": node_id,
                "contract_id": contract_id,
                "activation": activation,
                "select": select,
                "publish": publish,
                "semantic_node_id": semantic_node_id,
            }
        )

    monkeypatch.setattr("graph_engine.stategraph.attempt_graph.add_attempt_node", spy)
    return recorded


def test_default_semantic_id_joins_namespace_and_node_id(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded = _record(monkeypatch)
    context = _Context()
    graph = AttemptGraph(_State, context, namespace="intake", activation=_select)
    assert graph.add_attempt("explore", "contract.explore", select=_select, publish=_publish) is graph
    assert recorded == [
        {
            "builder": graph,
            "context": context,
            "node_id": "explore",
            "contract_id": "contract.explore",
            "activation": _select,
            "select": _select,
            "publish": _publish,
            "semantic_node_id": "intake.explore",
        }
    ]


def test_explicit_semantic_node_id_is_not_prefixed(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded = _record(monkeypatch)
    graph = AttemptGraph(_State, _Context(), namespace="intake", activation=_select)
    graph.add_attempt(
        "intake.case-design",
        "contract.design",
        select=_select,
        publish=_publish,
        semantic_node_id="intake.case-design",
    )
    assert recorded[0]["semantic_node_id"] == "intake.case-design"
    assert recorded[0]["semantic_node_id"] != "intake.intake.case-design"


def test_graph_activation_is_used_until_a_call_overrides_it(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded = _record(monkeypatch)
    graph_activation = object()
    call_activation = object()
    graph = AttemptGraph(_State, _Context(), namespace="intake", activation=graph_activation)
    graph.add_attempt("intake", "contract.intake", select=_select, publish=_publish)
    graph.add_attempt(
        "explore",
        "contract.explore",
        select=_select,
        publish=_publish,
        activation=call_activation,
    )
    assert [item["activation"] for item in recorded] == [graph_activation, call_activation]


def test_call_activation_supplies_a_missing_graph_default(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded = _record(monkeypatch)
    call_activation = object()
    graph = AttemptGraph(_State, _Context(), namespace="intake")
    graph.add_attempt(
        "intake",
        "contract.intake",
        select=_select,
        publish=_publish,
        activation=call_activation,
    )
    assert recorded[0]["activation"] is call_activation


def test_missing_activation_names_the_node(monkeypatch: pytest.MonkeyPatch) -> None:
    _record(monkeypatch)
    graph = AttemptGraph(_State, _Context(), namespace="intake")
    with pytest.raises(ValueError, match="missing-node"):
        graph.add_attempt("missing-node", "contract.missing", select=_select, publish=_publish)


def test_str_and_object_contracts_reach_add_attempt_node(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded = _record(monkeypatch)
    contract = _Contract("object.contract")
    graph = AttemptGraph(_State, _Context(), namespace="intake", activation=_select)
    graph.add_attempt("from-str", "string.contract", select=_select, publish=_publish)
    graph.add_attempt("from-object", contract, select=_select, publish=_publish)
    assert [item["contract_id"] for item in recorded] == ["string.contract", "object.contract"]
    assert [item["node_id"] for item in recorded] == ["from-str", "from-object"]


@pytest.mark.parametrize("namespace", ["", ".", "..", ".intake", "intake.", ".intake."])
def test_empty_or_dotted_namespace_is_rejected(namespace: str) -> None:
    with pytest.raises(ValueError, match="namespace"):
        AttemptGraph(_State, _Context(), namespace=namespace)


def test_add_attempt_edge_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[object, str, str, str]] = []

    def spy(builder: object, source: str, target: str, *, on_failure: str) -> None:
        seen.append((builder, source, target, on_failure))

    monkeypatch.setattr("graph_engine.stategraph.routing.add_attempt_edge", spy)
    graph = AttemptGraph(_State, _Context(), namespace="intake", activation=_select)
    assert graph.add_attempt_edge("case-repair", "case-review", on_failure="exhausted") is graph
    assert seen == [(graph, "case-repair", "case-review", "exhausted")]


def test_add_route_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[object, ...]] = []

    def route(state: Mapping[str, object]) -> str:
        del state
        return "case-review"

    def spy(
        builder: object,
        source: str,
        route_fn: object,
        *,
        targets: Iterable[str],
        on_failure: str | None = None,
    ) -> None:
        seen.append((builder, source, route_fn, tuple(targets), on_failure))

    monkeypatch.setattr("graph_engine.stategraph.routing.add_route", spy)
    graph = AttemptGraph(_State, _Context(), namespace="intake", activation=_select)
    assert graph.add_route("case-design", route, targets=("case-review", "failed")) is graph
    assert seen == [(graph, "case-design", route, ("case-review", "failed"), None)]


def test_output_schema_reaches_state_graph() -> None:
    class _Output(TypedDict, total=False):
        value: str

    graph = AttemptGraph(_State, _Context(), namespace="generation", output_schema=_Output)
    assert graph.output_schema is _Output


def test_compile_subgraph_passes_the_same_builder() -> None:
    context = _Context()
    graph = AttemptGraph(_State, context, namespace="intake", activation=_select)
    graph.add_node("done", lambda state: state)
    graph.add_edge(START, "done")
    graph.add_edge("done", END)
    compiled = graph.compile_subgraph()
    assert context.builders == [graph]
    assert isinstance(compiled, CompiledStateGraph)


def test_langgraph_mutation_methods_stay_on_state_graph() -> None:
    assert AttemptGraph.add_node is StateGraph.add_node
    assert AttemptGraph.add_edge is StateGraph.add_edge
    assert AttemptGraph.add_conditional_edges is StateGraph.add_conditional_edges
