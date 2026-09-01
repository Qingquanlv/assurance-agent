from __future__ import annotations

from typing import Any, cast
from unittest.mock import patch

import pytest
from langchain_core.runnables.config import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphInterrupt
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command
from pydantic import ValidationError

from assurance_product.graphs.execute import (
    adapt_quality_assess,
    coverage_human_interrupt,
    resume_product_interrupts,
    route_coverage_decision,
)
from assurance_product.graphs.factory import build_product_graphs
from assurance_product.graphs.state import COVERAGE_DECISION_ACTIONS, ProductState
from graph_engine.boot.boot import EngineGraphBuildContext

from tests.product.test_product_stategraph_flow import _flow_features, _public_input

_COVERAGE_INTERRUPT_ID = "product-coverage-decision"
_FEATURE_INTERRUPT_ID = "improvement-apply-human-review"


def _config() -> RunnableConfig:
    return {
        "configurable": {
            "thread_id": "inv-1",
            "assurance_revision_id": "a" * 64,
            "assurance_product_lock_digest": "b" * 64,
            "assurance_root_input_digest": "c" * 64,
            "assurance_fencing_token": 1,
            "assurance_entrypoint": "execute",
        }
    }


def test_coverage_decision_accepts_only_approve_or_reject() -> None:
    assert COVERAGE_DECISION_ACTIONS == ("approve", "reject")
    state = cast(ProductState, {"change_id": "CH-DEMO-001", "coverage_state": "needs_human"})
    with patch("assurance_product.graphs.execute.interrupt", return_value={"action": "approve"}):
        assert coverage_human_interrupt(state) == {
            "coverage_decision": "approve",
            "human_action": "approve",
            "coverage_state": "satisfied",
        }
    with patch("assurance_product.graphs.execute.interrupt", return_value={"action": "reject"}):
        assert coverage_human_interrupt(state) == {"coverage_decision": "reject", "human_action": "reject"}
    with patch("assurance_product.graphs.execute.interrupt", return_value={"action": "request_rework"}):
        with pytest.raises(ValidationError):
            coverage_human_interrupt(state)
    with patch("assurance_product.graphs.execute.interrupt", return_value={"action": "hold"}):
        with pytest.raises(ValidationError):
            coverage_human_interrupt(state)
    assert route_coverage_decision({"coverage_decision": "approve"}) == "assess-satisfied"
    assert route_coverage_decision({"coverage_decision": "reject"}) == "not-achieved"


def test_coverage_interrupt_validates_after_restart_and_does_not_mutate_before_interrupt() -> None:
    seen: list[object] = []

    def _first(payload: object) -> object:
        seen.append(payload)
        raise RuntimeError("interrupt")

    state = cast(
        ProductState,
        {"change_id": "CH-DEMO-001", "coverage_state": "needs_human", "decision": "leftover"},
    )
    with patch("assurance_product.graphs.execute.interrupt", side_effect=_first):
        with pytest.raises(RuntimeError, match="interrupt"):
            coverage_human_interrupt(state)
    request = seen[0]
    assert isinstance(request, dict)
    assert set(request["actions"]) == {"approve", "reject"}
    assert request["interrupt_id"] == _COVERAGE_INTERRUPT_ID
    assert request["reason"] == "coverage_needs_human"
    assert request["ordinal"] == 0
    with patch("assurance_product.graphs.execute.interrupt", return_value={"action": "approve"}):
        update = coverage_human_interrupt(state)
        assert update == {
            "coverage_decision": "approve",
            "human_action": "approve",
            "coverage_state": "satisfied",
        }
        assert "decision" not in update
        assert update["coverage_state"] == "satisfied"


def test_schema_adapter_is_deterministic_and_does_not_invoke_child() -> None:
    first = adapt_quality_assess(
        cast(
            ProductState,
            {
                "schema_version": "1",
                "change_id": "CH-DEMO-001",
                "requirement": "Add login",
                "run_mode": "case",
                "selected_test_families": ["api"],
                "capability_leafs": ["entities.item.create"],
                "capability_catalog": {
                    "resource_id": "assurance.product.configuration.capability-catalog",
                    "sha256": "a" * 64,
                },
                "product_policy": {
                    "resource_id": "assurance.product.configuration.product-policy",
                    "sha256": "a" * 64,
                },
                "data_knowledge": {
                    "resource_id": "assurance.product.configuration.data-knowledge",
                    "sha256": "a" * 64,
                },
                "allowed_artifact_paths": ["qa/changes"],
                "budgets": {
                    "review_rounds": 1,
                    "coverage_rounds": 2,
                    "healing_rounds": 1,
                    "execution_retries": 1,
                },
                "artifacts": [],
                "decision": "pass",
            },
        )
    )
    second = adapt_quality_assess(cast(ProductState, first))
    assert first == second
    assert first["rounds_used"] == 0
    assert first["rounds_budget"] == 2
    assert "interrupt" not in first


def test_multiple_interrupts_resume_via_interrupt_id_mapping() -> None:
    saver = InMemorySaver()

    def _feature_interrupt(state: object) -> dict[str, object]:
        del state
        from langgraph.types import interrupt

        raw = interrupt(
            {
                "reason": "needs_human_review",
                "actions": ["approve", "reject"],
                "interrupt_id": _FEATURE_INTERRUPT_ID,
                "ordinal": 1,
            }
        )
        action = raw["action"] if isinstance(raw, dict) else raw
        return {"feature_action": action}

    builder: StateGraph[ProductState] = StateGraph(ProductState)
    builder.add_node("coverage-human", cast(Any, coverage_human_interrupt))
    builder.add_node("feature-human", cast(Any, _feature_interrupt))
    builder.add_edge(START, "coverage-human")
    builder.add_edge(START, "feature-human")
    builder.add_edge("coverage-human", END)
    builder.add_edge("feature-human", END)
    graph = builder.compile(checkpointer=saver)
    config = _config()
    try:
        graph.invoke({"change_id": "CH-DEMO-001", "coverage_state": "needs_human"}, config=config)
    except GraphInterrupt as error:
        assert error.args
    snapshot = graph.get_state(config)
    pending = snapshot.tasks
    assert len(pending) >= 1
    with pytest.raises((ValueError, TypeError)):
        resume_product_interrupts(graph, config, "approve")
    resumed = resume_product_interrupts(
        graph,
        config,
        {
            _COVERAGE_INTERRUPT_ID: {"action": "approve"},
            _FEATURE_INTERRUPT_ID: {"action": "reject"},
        },
    )
    assert resumed["coverage_decision"] == "approve"
    assert resumed["feature_action"] == "reject"


def test_execute_coverage_human_resume_approve() -> None:
    graphs = build_product_graphs(
        context=EngineGraphBuildContext(contracts={}, checkpointer=InMemorySaver(), approved_source_roots=()),
        features=_flow_features(
            assess={"coverage_state": "needs_human", "rounds_used": 0, "rounds_budget": 1}
        ),
    )
    graph = graphs.entrypoints["execute"]
    config = _config()
    try:
        graph.invoke(_public_input("execute"), config=config)
    except GraphInterrupt:
        pass
    resumed = graph.invoke(Command(resume={"action": "approve"}), config=config)
    assert resumed["coverage_decision"] == "approve"
    assert resumed["terminal"] in {"done", "achieved"}
