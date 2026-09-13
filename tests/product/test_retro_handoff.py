from __future__ import annotations

from dataclasses import replace
from typing import Any, cast

import pytest
from langgraph.graph import END, START, StateGraph

from assurance_improvement.contracts.retro import RetroBuildSlicesInputV1
from assurance_improvement.graphs.factory import ImprovementGraphs
from assurance_improvement.graphs.nodes import select_build_slices
from assurance_improvement.graphs.state import ImprovementState
from assurance_product.graphs.factory import invoke_product_root

from tests.product.test_product_stategraph_flow import _flow_features, _product_graphs, _public_input


@pytest.mark.parametrize("explicit_window", [False, True])
def test_retro_root_preserves_selection_into_build_slices(explicit_window: bool) -> None:
    selected: list[RetroBuildSlicesInputV1] = []

    def consume(state: ImprovementState) -> dict[str, object]:
        selected.append(select_build_slices(state))
        return {"status": "done"}

    child = StateGraph(ImprovementState)
    child.add_node("consume", cast(Any, consume))
    child.add_edge(START, "consume")
    child.add_edge("consume", END)
    features = _flow_features()
    features["assurance.improvement"] = replace(
        cast(ImprovementGraphs, features["assurance.improvement"]), retro=child.compile()
    )
    payload = _public_input("retro")
    expected_changes = ("CH-DEMO-001", "CH-DEMO-002") if explicit_window else ("CH-DEMO-001",)
    if explicit_window:
        payload["retro_window"] = {
            "selection": {"mode": "change_ids", "requested_change_ids": list(expected_changes)},
            "change_ids": list(expected_changes),
        }
    ref = {"path": "qa/results/report/report.md", "digest": "a" * 64}
    payload["artifacts"] = [ref]

    invoke_product_root(_product_graphs(features), "retro", payload)

    assert len(selected) == 1
    assert selected[0].retro_id.startswith("retro-")
    assert selected[0].window.change_ids == expected_changes
    assert [item.model_dump(mode="json") for item in selected[0].source_refs] == [ref]


def test_failed_retro_is_failed_at_application_status_boundary() -> None:
    from types import SimpleNamespace
    from graph_engine.application.application import _status_from_snapshot

    child = StateGraph(ImprovementState)
    child.add_node("fail", lambda state: {"status": "failed"})
    child.add_edge(START, "fail")
    child.add_edge("fail", END)
    features = _flow_features()
    features["assurance.improvement"] = replace(
        cast(ImprovementGraphs, features["assurance.improvement"]), retro=child.compile()
    )
    result = invoke_product_root(_product_graphs(features), "retro", _public_input("retro"))
    status = _status_from_snapshot(SimpleNamespace(values=result, next=(), interrupts=()))
    assert status.status == "failed"
