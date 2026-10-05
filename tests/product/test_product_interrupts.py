"""Multiple pending interrupts resume by LangGraph interrupt id."""

from __future__ import annotations

from typing import Any, TypedDict, cast

import pytest
from langchain_core.runnables.config import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt


class _ToyState(TypedDict, total=False):
    change_id: str
    human_action: str
    decision: str


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


def test_multiple_feature_interrupts_resume_by_langgraph_interrupt_id() -> None:
    builder: StateGraph[_ToyState] = StateGraph(_ToyState)
    builder.add_node("case-human", cast(Any, _human_node("full.human-review", "human_action")))
    builder.add_node("improvement-human", cast(Any, _human_node("full.approval", "decision")))
    builder.add_edge(START, "case-human")
    builder.add_edge(START, "improvement-human")
    builder.add_edge("case-human", END)
    builder.add_edge("improvement-human", END)
    graph = builder.compile(checkpointer=InMemorySaver())
    config = _config()
    graph.invoke({"change_id": "CH-DEMO-001"}, config=config)
    snapshot = graph.get_state(config)
    pending = {item.id: item.value["interrupt_id"] for item in snapshot.interrupts}
    assert set(pending.values()) == {"full.human-review", "full.approval"}
    assert set(pending) != set(pending.values())
    with pytest.raises(Exception):
        graph.invoke(Command(resume="approve"), config=config)
    by_logical = {logical: graph_id for graph_id, logical in pending.items()}
    resumed = graph.invoke(
        Command(
            resume={
                by_logical["full.human-review"]: {"action": "approve"},
                by_logical["full.approval"]: {"action": "reject"},
            }
        ),
        config=config,
    )
    assert resumed["human_action"] == "approve"
    assert resumed["decision"] == "reject"
