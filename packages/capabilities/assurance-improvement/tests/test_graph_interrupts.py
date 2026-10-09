from __future__ import annotations

from typing import Any, cast

import pytest
from langchain_core.runnables.config import RunnableConfig
from langgraph.errors import GraphInterrupt
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from assurance_improvement.contracts.decisions import APPLY_HUMAN_ACTIONS
from assurance_improvement.graphs.factory import build_improvement_graphs as _build_improvement_graphs
from graph_engine.testing import GraphHarness, committed
from graph_engine.testing.graph_harness import _prepare_anchored_backend

from test_graph_delivery import (  # type: ignore[import-not-found]
    _ApplyChannels,
    apply_graph_input,
    apply_receipt,
    auto_review_output,
    evaluate_receipt,
    human_review_output,
)
from test_improvement_graph_factory import (  # type: ignore[import-not-found]
    improvement_contracts,
)

from graph_engine.testing.feature_bundle import compile_bundle


def build_improvement_graphs(*args, **kwargs):
    return compile_bundle(_build_improvement_graphs(*args, **kwargs))


_INTERRUPT_ID = "improvement.human-review"


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


@pytest.mark.parametrize("action", ("approve", "reject", "request_rework", "supersede"))
async def test_apply_human_review_resume_reuses_interrupt_identity(action: str) -> None:
    from graph_engine.attempts.models.resolutions import ReceiptRef

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

    wrapper: StateGraph[_ApplyChannels] = StateGraph(_ApplyChannels)
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
    assert _interrupt_value(resumed) is None
    if action != "approve":
        assert resumed.get("status") in {"rejected", "rework", "superseded", "failed"}


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
