from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import pytest
from langchain_core.runnables.config import RunnableConfig
from langgraph.errors import GraphInterrupt
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command
from pydantic import ValidationError

from assurance_improvement.graphs.factory import build_improvement_graphs
from assurance_improvement.graphs.nodes import APPLY_HUMAN_ACTIONS, apply_human_interrupt
from assurance_improvement.graphs.state import ImprovementState
from graph_engine.testing import GraphHarness, committed
from graph_engine.testing.graph_harness import _prepare_anchored_backend

from test_graph_delivery import (  # type: ignore[import-not-found]
    apply_graph_input,
    apply_receipt,
    auto_review_output,
    evaluate_receipt,
    human_review_output,
)
from test_improvement_graph_factory import (  # type: ignore[import-not-found]
    EFFECT_IDS,
    improvement_contracts,
)

_GRAPHS_ROOT = Path(__file__).resolve().parents[1] / "assurance_improvement" / "graphs"
_INTERRUPT_ID = "improvement-apply-human-review"
_INTERRUPT_REASON = "needs_human_review"


def _config() -> RunnableConfig:
    return {
        "configurable": {
            "thread_id": "inv-1",
            "assurance_revision_id": "a" * 64,
            "assurance_product_lock_digest": "b" * 64,
            "assurance_root_input_digest": "c" * 64,
            "assurance_fencing_token": 1,
            "assurance_entrypoint": "improvement-apply",
        }
    }


def test_apply_interrupt_accepts_exactly_the_four_human_actions() -> None:
    assert APPLY_HUMAN_ACTIONS == ("approve", "reject", "request_rework", "supersede")
    state = apply_graph_input(lifecycle_state="proposed")
    with patch("assurance_improvement.graphs.nodes.interrupt", return_value={"action": "hold"}):
        with pytest.raises(ValidationError):
            apply_human_interrupt(state)
    with patch("assurance_improvement.graphs.nodes.interrupt", return_value={"action": "approve"}):
        assert apply_human_interrupt(state) == {"human_action": "approve"}
    with patch("assurance_improvement.graphs.nodes.interrupt", return_value={"action": "reject"}):
        assert apply_human_interrupt(state) == {"human_action": "reject"}
    with patch("assurance_improvement.graphs.nodes.interrupt", return_value={"action": "request_rework"}):
        assert apply_human_interrupt(state) == {"human_action": "request_rework"}
    with patch("assurance_improvement.graphs.nodes.interrupt", return_value={"action": "supersede"}):
        assert apply_human_interrupt(state) == {"human_action": "supersede"}


def test_interrupt_validates_after_restart_and_preserves_identity() -> None:
    seen: list[object] = []

    def _first(payload: object) -> object:
        seen.append(payload)
        raise RuntimeError("interrupt")

    state = apply_graph_input(lifecycle_state="proposed", decision="leftover")
    with patch("assurance_improvement.graphs.nodes.interrupt", side_effect=_first):
        with pytest.raises(RuntimeError, match="interrupt"):
            apply_human_interrupt(state)
    request = seen[0]
    assert isinstance(request, dict)
    assert set(request["actions"]) == set(APPLY_HUMAN_ACTIONS)
    assert request["interrupt_id"] == _INTERRUPT_ID
    assert request["ordinal"] == 0
    assert request["reason"] == _INTERRUPT_REASON

    with patch("assurance_improvement.graphs.nodes.interrupt", return_value={"action": "approve"}):
        update = apply_human_interrupt(state)
    assert update == {"human_action": "approve"}
    assert "decision" not in update
    assert "lifecycle_state" not in update
    assert "effect_refs" not in update
    assert "receipt_refs" not in update

    restart_seen: list[object] = []

    def _restart(payload: object) -> object:
        restart_seen.append(payload)
        raise RuntimeError("interrupt")

    with patch("assurance_improvement.graphs.nodes.interrupt", side_effect=_restart):
        with pytest.raises(RuntimeError, match="interrupt"):
            apply_human_interrupt(state)
    restart_request = restart_seen[0]
    assert isinstance(restart_request, dict)
    assert restart_request["interrupt_id"] == _INTERRUPT_ID
    assert restart_request["ordinal"] == 0
    assert restart_request["reason"] == _INTERRUPT_REASON


def test_interrupt_node_has_no_effect_kinds_or_pre_interrupt_side_effect() -> None:
    nodes = (_GRAPHS_ROOT / "nodes.py").read_text(encoding="utf-8")
    tree = ast.parse(nodes, filename=str(_GRAPHS_ROOT / "nodes.py"))
    review_fn = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "apply_human_interrupt"
    )
    source = ast.get_source_segment(nodes, review_fn) or ""
    for effect_id in EFFECT_IDS:
        assert effect_id not in source
    assert "effect" not in source
    assert "system_wake" not in source
    assert "system_block" not in source


def test_pending_kernel_effects_are_not_this_human_decision_node() -> None:
    nodes = (_GRAPHS_ROOT / "nodes.py").read_text(encoding="utf-8")
    tree = ast.parse(nodes, filename=str(_GRAPHS_ROOT / "nodes.py"))
    review_fn = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "apply_human_interrupt"
    )
    source = ast.get_source_segment(nodes, review_fn) or ""
    assert "Attempt" not in source
    assert "settle" not in source


@pytest.mark.parametrize("action", ("approve", "reject", "request_rework", "supersede"))
async def test_apply_human_review_resume_reuses_interrupt_identity(action: str) -> None:
    from graph_engine.attempts.resolutions import ReceiptRef

    harness = GraphHarness()
    backend = harness.anchored_memory_checkpointer()
    await _prepare_anchored_backend(backend)
    bundle = build_improvement_graphs(
        harness.recording_context(owner_id="assurance.improvement", contracts=improvement_contracts())
    )
    harness._kernel.load_script(
        {
            "improvement.apply-auto-review": [
                committed(
                    auto_review_output(lifecycle_state="proposed"),
                    ReceiptRef(receipt_id="receipt-1", receipt_digest="a" * 64),
                )
            ],
            "improvement.apply-human-review": [
                committed(
                    human_review_output(
                        lifecycle_state={
                            "approve": "approved",
                            "reject": "rejected",
                            "request_rework": "needs_rework",
                            "supersede": "superseded",
                        }[action]
                    ),
                    ReceiptRef(receipt_id="receipt-2", receipt_digest="a" * 64),
                )
            ],
            "improvement.apply-evaluate": [
                committed(evaluate_receipt(), ReceiptRef(receipt_id="receipt-3", receipt_digest="a" * 64))
            ],
            "improvement.apply": [
                committed(apply_receipt(), ReceiptRef(receipt_id="receipt-4", receipt_digest="a" * 64))
            ],
        }
    )
    wrapper: StateGraph[ImprovementState] = StateGraph(ImprovementState)
    wrapper.add_node("apply", cast(Any, bundle.apply))
    wrapper.add_edge(START, "apply")
    wrapper.add_edge("apply", END)
    graph = wrapper.compile(checkpointer=backend)
    config = _config()
    interrupted: object | None
    try:
        interrupted = await graph.ainvoke(cast(Any, apply_graph_input()), config=config)
    except GraphInterrupt as error:
        interrupted = error
    value = _interrupt_value(interrupted)
    if isinstance(value, dict):
        assert value.get("interrupt_id") == _INTERRUPT_ID
        assert value.get("ordinal") == 0
        assert set(value.get("actions") or ()) == set(APPLY_HUMAN_ACTIONS)
    resumed = await graph.ainvoke(Command(resume={"action": action}), config=config)
    assert resumed["human_action"] == action
    assert _interrupt_value(resumed) is None
    if action != "approve":
        assert resumed.get("lifecycle_state") in {
            "rejected",
            "needs_rework",
            "superseded",
            action,
        }


def _interrupt_value(result: object) -> object | None:
    if isinstance(result, GraphInterrupt):
        interrupts = getattr(result, "args", ())
        if interrupts:
            first = interrupts[0]
            if isinstance(first, tuple) and first:
                return getattr(first[0], "value", first[0])
            return getattr(first, "value", first)
        return None
    if not isinstance(result, dict):
        return None
    interrupts = result.get("__interrupt__")
    if not interrupts:
        return None
    first = interrupts[0]
    return getattr(first, "value", first)
