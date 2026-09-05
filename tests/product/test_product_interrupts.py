from __future__ import annotations

from typing import Any, cast

import pytest
from langchain_core.runnables.config import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from assurance_product.graphs.execute import resume_product_interrupts
from assurance_product.graphs.state import ProductState

_FIRST_ID = "case-human-review"
_SECOND_ID = "improvement-apply-human-review"


def _config() -> RunnableConfig:
    return {
        "configurable": {
            "thread_id": "inv-1",
            "assurance_revision_id": "a" * 64,
            "assurance_product_lock_digest": "b" * 64,
            "assurance_root_input_digest": "c" * 64,
            "assurance_fencing_token": 1,
            "assurance_entrypoint": "full",
        }
    }


def _human_node(interrupt_id: str, output_key: str):
    def node(state: object) -> dict[str, object]:
        del state
        raw = interrupt(
            {
                "reason": "needs_human_review",
                "actions": ["approve", "reject"],
                "interrupt_id": interrupt_id,
                "ordinal": 0,
            }
        )
        action = raw["action"] if isinstance(raw, dict) else raw
        return {output_key: action}

    return node


def test_multiple_feature_interrupts_resume_by_logical_interrupt_id() -> None:
    builder: StateGraph[ProductState] = StateGraph(ProductState)
    builder.add_node("case-human", cast(Any, _human_node(_FIRST_ID, "human_action")))
    builder.add_node("improvement-human", cast(Any, _human_node(_SECOND_ID, "feature_action")))
    builder.add_edge(START, "case-human")
    builder.add_edge(START, "improvement-human")
    builder.add_edge("case-human", END)
    builder.add_edge("improvement-human", END)
    graph = builder.compile(checkpointer=InMemorySaver())
    config = _config()
    graph.invoke({"change_id": "CH-DEMO-001"}, config=config)
    with pytest.raises(ValueError, match="scalar resume rejected"):
        resume_product_interrupts(graph, config, "approve")
    resumed = resume_product_interrupts(
        graph,
        config,
        {_FIRST_ID: {"action": "approve"}, _SECOND_ID: {"action": "reject"}},
    )
    assert resumed["human_action"] == "approve"
    assert resumed["feature_action"] == "reject"
