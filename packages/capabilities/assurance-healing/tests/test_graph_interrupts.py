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

from assurance_healing.graphs.factory import build_healing_graphs
from assurance_healing.graphs.nodes import COVERAGE_REVIEW_ACTIONS, coverage_review
from assurance_healing.graphs.state import HealingState
from graph_engine.testing import GraphHarness, committed
from graph_engine.testing.graph_harness import _prepare_anchored_backend

from test_graph_factory import (  # type: ignore[import-not-found]
    EFFECT_IDS,
    coverage_agent_output,
    coverage_graph_input,
    failure_agent_output,
    failure_graph_input,
    healing_contracts,
)

_GRAPHS_ROOT = Path(__file__).resolve().parents[1] / "assurance_healing" / "graphs"
_SHA = "a" * 64
_INTERRUPT_ID = "coverage-repair-needs-review"
_INTERRUPT_REASON = "coverage_repair_needs_review"


def _config() -> RunnableConfig:
    return {
        "configurable": {
            "thread_id": "inv-1",
            "assurance_revision_id": "a" * 64,
            "assurance_product_lock_digest": "b" * 64,
            "assurance_root_input_digest": "c" * 64,
            "assurance_fencing_token": 1,
            "assurance_entrypoint": "repair-coverage",
        }
    }


def test_coverage_review_accepts_only_approve_reject() -> None:
    assert COVERAGE_REVIEW_ACTIONS == ("approve", "reject")
    state = {"kind": "coverage", "status": "needs_review", "rounds_used": 2, "rounds_budget": 4}
    with patch("assurance_healing.graphs.nodes.interrupt", return_value={"action": "request_rework"}):
        with pytest.raises(ValidationError):
            coverage_review(state)
    with patch("assurance_healing.graphs.nodes.interrupt", return_value={"action": "hold"}):
        with pytest.raises(ValidationError):
            coverage_review(state)
    with patch("assurance_healing.graphs.nodes.interrupt", return_value={"action": "approve"}):
        assert coverage_review(state) == {"human_action": "approve"}
    with patch("assurance_healing.graphs.nodes.interrupt", return_value={"action": "reject"}):
        assert coverage_review(state) == {"human_action": "reject"}


def test_interrupt_node_validates_after_restart_and_does_not_mutate_before_interrupt() -> None:
    seen: list[object] = []

    def _first(payload: object) -> object:
        seen.append(payload)
        raise RuntimeError("interrupt")

    state = {
        "kind": "coverage",
        "status": "needs_review",
        "rounds_used": 2,
        "rounds_budget": 4,
        "decision": "leftover",
    }
    with patch("assurance_healing.graphs.nodes.interrupt", side_effect=_first):
        with pytest.raises(RuntimeError, match="interrupt"):
            coverage_review(state)
    assert seen
    request = seen[0]
    assert isinstance(request, dict)
    assert set(request["actions"]) == {"approve", "reject"}
    assert request["interrupt_id"] == _INTERRUPT_ID
    assert request["ordinal"] == 0
    assert request["reason"] == _INTERRUPT_REASON

    with patch("assurance_healing.graphs.nodes.interrupt", return_value={"action": "approve"}):
        update = coverage_review(state)
    assert update == {"human_action": "approve"}
    assert "decision" not in update
    assert "status" not in update
    assert "rounds_used" not in update
    assert "effect_refs" not in update

    restart_seen: list[object] = []

    def _restart(payload: object) -> object:
        restart_seen.append(payload)
        raise RuntimeError("interrupt")

    with patch("assurance_healing.graphs.nodes.interrupt", side_effect=_restart):
        with pytest.raises(RuntimeError, match="interrupt"):
            coverage_review(state)
    restart_request = restart_seen[0]
    assert isinstance(restart_request, dict)
    assert restart_request["interrupt_id"] == _INTERRUPT_ID
    assert restart_request["ordinal"] == 0
    assert restart_request["reason"] == _INTERRUPT_REASON


def test_interrupt_node_has_no_effect_kinds_or_pre_interrupt_side_effect() -> None:
    nodes = (_GRAPHS_ROOT / "nodes.py").read_text(encoding="utf-8")
    tree = ast.parse(nodes, filename=str(_GRAPHS_ROOT / "nodes.py"))
    review_fn = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "coverage_review"
    )
    source = ast.get_source_segment(nodes, review_fn) or ""
    for effect_id in EFFECT_IDS:
        assert effect_id not in source
    assert "effect" not in source


def test_kernel_protocol_receipts_use_exactly_the_three_healing_effect_ids() -> None:
    from graph_engine import RegistryPorts
    from graph_engine.effects.contracts import EXPECTED_EFFECT_KINDS

    from assurance_healing.contracts.attempts import HEALING_EFFECT_IDS
    from assurance_healing.plugin import HealingPlugin

    contribution = HealingPlugin.contribute(RegistryPorts(engine_api="2.0"))
    protocol_kinds = tuple(sorted(item.kind for item in contribution.effects))
    receipt_schemas = tuple(sorted(item.receipt_schema_id for item in contribution.effects))
    kernel_healing = tuple(
        sorted(kind for kind in EXPECTED_EFFECT_KINDS if kind.startswith("assurance.healing."))
    )
    expected = tuple(sorted(HEALING_EFFECT_IDS))
    assert expected == (
        "assurance.healing.effect.allocation.v2",
        "assurance.healing.effect.heal-apply.v2",
        "assurance.healing.effect.proposal-approved.v1",
    )
    assert protocol_kinds == expected
    assert kernel_healing == expected
    assert receipt_schemas == (
        "assurance.healing.schema.allocation-receipt.v2",
        "assurance.healing.schema.heal-apply-receipt.v2",
        "assurance.healing.schema.proposal-approved-receipt.v1",
    )
    assert set(EFFECT_IDS) == set(expected)


async def test_published_effect_refs_come_from_kernel_receipt_not_output_extras() -> None:
    from graph_engine.attempts.resolutions import ReceiptRef

    harness = GraphHarness()
    context = harness.recording_context(owner_id="assurance.healing", contracts=healing_contracts())
    bundle = build_healing_graphs(context)
    receipt = ReceiptRef(receipt_id="receipt-1", receipt_digest=_SHA)
    failure_output = failure_agent_output()
    failure_output["effect_refs"] = [{"kind": "forged.failure.effect", "digest": _SHA}]
    coverage_output = coverage_agent_output()
    coverage_output["effect_refs"] = [{"kind": "forged.coverage.effect", "digest": _SHA}]
    failure = await harness.run(
        bundle.repair_failure,
        input=failure_graph_input(),
        script={"healing.fix-proposal": [committed(failure_output, receipt)]},
    )
    coverage = await harness.run(
        bundle.repair_coverage,
        input=coverage_graph_input(),
        script={"healing.coverage-repair": [committed(coverage_output, receipt)]},
    )
    for result in (failure, coverage):
        published = result.published_update
        assert published is not None
        refs = published["effect_refs"]
        assert isinstance(refs, list)
        assert refs == []
        assert not any(item.get("kind", "").startswith("forged.") for item in refs)


@pytest.mark.parametrize("action", ("approve", "reject"))
async def test_coverage_review_resume_reuses_interrupt_identity(action: str) -> None:
    harness = GraphHarness()
    backend = harness.anchored_memory_checkpointer()
    await _prepare_anchored_backend(backend)
    bundle = build_healing_graphs(
        harness.recording_context(owner_id="assurance.healing", contracts=healing_contracts())
    )
    from graph_engine.attempts.resolutions import ReceiptRef

    harness._kernel.load_script(
        {
            "healing.coverage-repair": [
                committed(
                    coverage_agent_output(status="needs_review"),
                    ReceiptRef(receipt_id="receipt-1", receipt_digest=_SHA),
                )
            ]
        }
    )
    wrapper: StateGraph[HealingState] = StateGraph(HealingState)
    wrapper.add_node("repair", cast(Any, bundle.repair_coverage))
    wrapper.add_edge(START, "repair")
    wrapper.add_edge("repair", END)
    graph = wrapper.compile(checkpointer=backend)
    config = _config()
    interrupted: object | None
    try:
        interrupted = await graph.ainvoke(cast(Any, coverage_graph_input()), config=config)
    except GraphInterrupt as error:
        interrupted = error
    else:
        value = _interrupt_value(interrupted)
        assert value is not None
    value = _interrupt_value(interrupted)
    if isinstance(value, dict):
        assert value.get("interrupt_id") == _INTERRUPT_ID
        assert value.get("ordinal") == 0
        assert set(value.get("actions") or ()) == {"approve", "reject"}
    resumed = await graph.ainvoke(Command(resume={"action": action}), config=config)
    assert resumed["human_action"] == action
    assert _interrupt_value(resumed) is None


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
