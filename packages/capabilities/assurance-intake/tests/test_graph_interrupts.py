from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast
from unittest.mock import patch

import pytest
from langchain_core.runnables.config import RunnableConfig
from langgraph.errors import GraphInterrupt
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command
from pydantic import ValidationError

from assurance_intake.contracts.agent import ArtifactListResultV1
from assurance_intake.contracts.attempts import AGENT_JOB_CONTRACTS
from assurance_intake.contracts.decisions import ReviewRoundAdvanceOutput, advance_review_round
from assurance_intake.contracts.review import CaseReviewResultV1
from assurance_intake.graphs.factory import build_intake_graphs

from assurance_intake.graphs.nodes import (
    HUMAN_REVIEW_ACTIONS,
    advance_review_round_node,
    human_review,
    human_review_retry,
    publish_case_review,
)
from assurance_intake.graphs.state import IntakeState
from graph_engine.attempts.contracts import TaskAttemptContract
from graph_engine.attempts.resolutions import ReceiptRef
from graph_engine.testing import GraphHarness, committed
from graph_engine.testing.graph_harness import _prepare_anchored_backend

_SHA = "a" * 64
_RECEIPT = ReceiptRef(receipt_id="receipt-1", receipt_digest=_SHA)


def _contracts() -> dict[str, TaskAttemptContract[Any, Any]]:
    return {contract.contract_id: contract.to_task_contract() for contract in AGENT_JOB_CONTRACTS.values()}


def _input() -> dict[str, object]:
    return {
        "change_id": "CH-DEMO-001",
        "requirement": "Cover department CRUD.",
        "selected_test_families": ["api"],
        "case_delta_paths": ["qa/changes/CH-DEMO-001/cases/menus/case.yaml"],
        "capability_leafs": ["entities.item.create"],
        "allowed_artifact_paths": ["qa/changes"],
        "rounds_used": 0,
        "rounds_budget": 2,
    }


def _artifact() -> ArtifactListResultV1:
    return ArtifactListResultV1(output_files=("qa/changes/CH-DEMO-001/proposal.md",))


def _design() -> dict[str, object]:
    return {
        "output_files": ["qa/changes/CH-DEMO-001/proposal.md"],
        "validation_status": "pass",
        "artifacts": [{"path": "qa/changes/CH-DEMO-001/proposal.md", "digest": _SHA}],
    }


def _review(
    decision: str,
    *,
    auto_fix: bool = False,
    human: bool = False,
    used: int = 0,
    budget: int = 2,
) -> dict[str, object]:
    return {
        "decision": decision,
        "auto_fix_allowed": auto_fix,
        "human_review_required": human,
        "artifacts": [{"path": "qa/changes", "digest": _SHA}],
        "rounds_used": used,
        "rounds_budget": budget,
    }


def _review_result_v1(
    decision: str,
    *,
    auto_fix: bool = False,
    human: bool = False,
) -> CaseReviewResultV1:
    return CaseReviewResultV1.model_validate(
        {
            "schema_version": "1.0",
            "review_type": "case",
            "change_id": "CH-DEMO-001",
            "decision": decision,
            "findings": [],
            "auto_fix_plan": [],
            "next_action": "continue",
            "auto_fix_allowed": auto_fix,
            "human_review_required": human,
            "risk_level": "low",
            "minimum_coverage": {
                "total_required": 0,
                "covered": 0,
                "skipped_by_scope": 0,
                "missing": [],
            },
            "source_verification": {
                "independent": True,
                "reviewed_source_files": ["src/app.py"],
                "verified_claims": [{"claim": "create item persists", "evidence_files": ["src/app.py"]}],
            },
        }
    )


def _config() -> RunnableConfig:
    return {
        "configurable": {
            "thread_id": "inv-1",
            "assurance_revision_id": "a" * 64,
            "assurance_product_lock_digest": "b" * 64,
            "assurance_root_input_digest": "c" * 64,
            "assurance_fencing_token": 1,
            "assurance_entrypoint": "prepare",
        }
    }


def test_publish_case_review_keeps_graph_rounds_when_result_omits_or_nulls_them() -> None:
    state = {"rounds_used": 0, "rounds_budget": 2}
    result = _review_result_v1("needs_fix", auto_fix=True)
    dumped = result.model_dump(mode="json")
    assert dumped["rounds_used"] is None
    assert dumped["rounds_budget"] is None
    published = publish_case_review(state, result, None)
    assert published["rounds_used"] == 0
    assert published["rounds_budget"] == 2
    omitted = {key: value for key, value in dumped.items() if key not in {"rounds_used", "rounds_budget"}}
    published_omitted = publish_case_review(state, omitted, None)
    assert published_omitted["rounds_used"] == 0
    assert published_omitted["rounds_budget"] == 2
    authored = {**dumped, "rounds_used": 1, "rounds_budget": 3}
    published_authored = publish_case_review(state, authored, None)
    assert published_authored["rounds_used"] == 1
    assert published_authored["rounds_budget"] == 3


def test_both_interrupt_sites_accept_only_approve_reject_request_rework() -> None:
    assert HUMAN_REVIEW_ACTIONS == ("approve", "reject", "request_rework")
    for node in (human_review, human_review_retry):
        with patch("assurance_intake.graphs.nodes.interrupt", return_value={"action": "supersede"}):
            with pytest.raises(ValidationError):
                node({"rounds_used": 0, "rounds_budget": 2})
        with patch("assurance_intake.graphs.nodes.interrupt", return_value={"action": "hold"}):
            with pytest.raises(ValidationError):
                node({"rounds_used": 0, "rounds_budget": 2})


def test_interrupt_node_validates_after_restart_and_does_not_mutate_before_interrupt() -> None:
    seen: list[object] = []

    def _first(payload: object) -> object:
        seen.append(payload)
        raise RuntimeError("interrupt")

    with patch("assurance_intake.graphs.nodes.interrupt", side_effect=_first):
        with pytest.raises(RuntimeError, match="interrupt"):
            human_review({"rounds_used": 0, "rounds_budget": 2, "decision": "needs_human_review"})
    assert seen
    request = seen[0]
    assert isinstance(request, dict)
    assert set(request["actions"]) == {"approve", "reject", "request_rework"}

    with patch("assurance_intake.graphs.nodes.interrupt", return_value={"action": "approve"}):
        update = human_review({"rounds_used": 0, "rounds_budget": 2, "decision": "needs_human_review"})
    assert update == {"human_action": "approve"}
    assert "decision" not in update
    assert "rounds_used" not in update


def test_advance_review_round_node_is_the_moved_pure_function() -> None:
    payload = {"rounds_used": 0, "rounds_budget": 2}
    output = advance_review_round_node(payload)
    expected = advance_review_round(payload)
    assert isinstance(expected, ReviewRoundAdvanceOutput)
    assert output["rounds_used"] == expected.rounds_used == 1
    assert output["rounds_budget"] == expected.rounds_budget == 2
    with pytest.raises((ValidationError, ValueError)):
        advance_review_round_node({"rounds_used": 2, "rounds_budget": 2})


async def test_pass_completes_without_advance() -> None:
    harness = GraphHarness()
    bundle = build_intake_graphs(
        harness.recording_context(owner_id="assurance.intake", contracts=_contracts())
    )
    result = await harness.run(
        bundle.prepare,
        input=_input(),
        script={
            "intake.intake": [committed(_artifact(), _RECEIPT)],
            "intake.explore": [committed(_artifact(), _RECEIPT)],
            "intake.case-design": [committed(_design(), _RECEIPT)],
            "intake.case-review": [committed(_review("pass"), _RECEIPT)],
        },
    )
    assert result.terminal is not None
    assert cast(dict[str, object], result.terminal).get("decision") in {"pass", "approved"}
    assert cast(dict[str, object], result.terminal).get("rounds_used") == 0


async def test_automatic_fix_advances_exactly_once() -> None:
    harness = GraphHarness()
    bundle = build_intake_graphs(
        harness.recording_context(owner_id="assurance.intake", contracts=_contracts())
    )
    result = await harness.run(
        bundle.prepare,
        input=_input(),
        script={
            "intake.intake": [committed(_artifact(), _RECEIPT)],
            "intake.explore": [committed(_artifact(), _RECEIPT)],
            "intake.case-design": [
                committed(_design(), _RECEIPT),
                committed(_design(), _RECEIPT),
            ],
            "intake.case-review": [
                committed(_review("needs_fix", auto_fix=True, used=0), _RECEIPT),
                committed(_review("pass", used=1), _RECEIPT),
            ],
        },
    )
    terminal = cast(dict[str, object], result.terminal)
    assert terminal.get("rounds_used") == 1
    assert terminal.get("decision") in {"pass", "approved"}
    assert [call.semantic_node_id for call in result.semantic_calls].count("intake.case-design") == 2


async def test_automatic_fix_advances_when_review_result_nulls_rounds() -> None:
    harness = GraphHarness()
    bundle = build_intake_graphs(
        harness.recording_context(owner_id="assurance.intake", contracts=_contracts())
    )
    result = await harness.run(
        bundle.prepare,
        input=_input(),
        script={
            "intake.intake": [committed(_artifact(), _RECEIPT)],
            "intake.explore": [committed(_artifact(), _RECEIPT)],
            "intake.case-design": [
                committed(_design(), _RECEIPT),
                committed(_design(), _RECEIPT),
            ],
            "intake.case-review": [
                committed(_review_result_v1("needs_fix", auto_fix=True), _RECEIPT),
                committed(_review_result_v1("pass"), _RECEIPT),
            ],
        },
    )
    terminal = cast(dict[str, object], result.terminal)
    assert terminal.get("rounds_used") == 1
    assert terminal.get("decision") in {"pass", "approved"}
    assert [call.semantic_node_id for call in result.semantic_calls].count("intake.case-design") == 2


async def test_reject_is_explicit_terminal() -> None:
    harness = GraphHarness()
    bundle = build_intake_graphs(
        harness.recording_context(owner_id="assurance.intake", contracts=_contracts())
    )
    result = await harness.run(
        bundle.prepare,
        input=_input(),
        script={
            "intake.intake": [committed(_artifact(), _RECEIPT)],
            "intake.explore": [committed(_artifact(), _RECEIPT)],
            "intake.case-design": [committed(_design(), _RECEIPT)],
            "intake.case-review": [committed(_review("reject"), _RECEIPT)],
        },
    )
    terminal = cast(dict[str, object], result.terminal)
    assert terminal.get("decision") == "reject"


async def test_budget_exhaustion_is_explicit_after_two_advances() -> None:
    harness = GraphHarness()
    bundle = build_intake_graphs(
        harness.recording_context(owner_id="assurance.intake", contracts=_contracts())
    )
    result = await harness.run(
        bundle.prepare,
        input=_input(),
        script={
            "intake.intake": [committed(_artifact(), _RECEIPT)],
            "intake.explore": [committed(_artifact(), _RECEIPT)],
            "intake.case-design": [
                committed(_design(), _RECEIPT),
                committed(_design(), _RECEIPT),
                committed(_design(), _RECEIPT),
            ],
            "intake.case-review": [
                committed(_review("needs_fix", auto_fix=True, used=0), _RECEIPT),
                committed(_review("needs_fix", auto_fix=True, used=1), _RECEIPT),
                committed(_review("needs_fix", auto_fix=True, used=2), _RECEIPT),
            ],
        },
    )
    terminal = cast(dict[str, object], result.terminal)
    assert terminal.get("decision") == "exhausted" or terminal.get("status") == "exhausted"
    assert terminal.get("rounds_used") == 2


def _interrupt_value(result: object) -> object | None:
    if not isinstance(result, dict):
        return None
    interrupts = result.get("__interrupt__")
    if not interrupts:
        return None
    first = interrupts[0]
    return getattr(first, "value", first)


async def test_request_rework_on_prepare_graph_advances_once_through_inbox() -> None:
    harness = GraphHarness()
    backend = harness.anchored_memory_checkpointer()
    await _prepare_anchored_backend(backend)
    bundle = build_intake_graphs(
        harness.recording_context(owner_id="assurance.intake", contracts=_contracts())
    )
    harness._kernel.load_script(
        {
            "intake.intake": [committed(_artifact(), _RECEIPT)],
            "intake.explore": [committed(_artifact(), _RECEIPT)],
            "intake.case-design": [committed(_design(), _RECEIPT), committed(_design(), _RECEIPT)],
            "intake.case-review": [
                committed(_review("needs_human_review", human=True), _RECEIPT),
                committed(_review("pass", used=1), _RECEIPT),
            ],
        }
    )
    wrapper: StateGraph[IntakeState] = StateGraph(IntakeState)
    wrapper.add_node("prepare", bundle.prepare)
    wrapper.add_edge(START, "prepare")
    wrapper.add_edge("prepare", END)
    graph = wrapper.compile(checkpointer=backend)
    config = _config()
    interrupted: object | None
    try:
        interrupted = await graph.ainvoke(_input(), config=config)
    except GraphInterrupt as error:
        interrupted = error
    else:
        assert _interrupt_value(interrupted) is not None
    resumed = await graph.ainvoke(Command(resume={"action": "request_rework"}), config=config)
    assert resumed["human_action"] == "request_rework"
    assert resumed["rounds_used"] == 1
    inbox = resumed["case_review_inbox"]
    current = inbox["current_trigger"]
    assert current is not None
    assert current["predecessor"] == "review-round-advance"
    assert current["value"] == {"rounds_used": 1, "rounds_budget": 2}
    assert resumed["current_trigger"] == current
    assert [call.semantic_node_id for call in harness._kernel.semantic_calls].count("intake.case-design") == 2
    assert resumed.get("decision") in {"pass", "approved"}


async def test_request_rework_advances_when_review_result_nulls_rounds() -> None:
    harness = GraphHarness()
    backend = harness.anchored_memory_checkpointer()
    await _prepare_anchored_backend(backend)
    bundle = build_intake_graphs(
        harness.recording_context(owner_id="assurance.intake", contracts=_contracts())
    )
    harness._kernel.load_script(
        {
            "intake.intake": [committed(_artifact(), _RECEIPT)],
            "intake.explore": [committed(_artifact(), _RECEIPT)],
            "intake.case-design": [committed(_design(), _RECEIPT), committed(_design(), _RECEIPT)],
            "intake.case-review": [
                committed(_review_result_v1("needs_human_review", human=True), _RECEIPT),
                committed(_review_result_v1("pass"), _RECEIPT),
            ],
        }
    )
    wrapper: StateGraph[IntakeState] = StateGraph(IntakeState)
    wrapper.add_node("prepare", bundle.prepare)
    wrapper.add_edge(START, "prepare")
    wrapper.add_edge("prepare", END)
    graph = wrapper.compile(checkpointer=backend)
    config = _config()
    interrupted: object | None
    try:
        interrupted = await graph.ainvoke(_input(), config=config)
    except GraphInterrupt as error:
        interrupted = error
    else:
        assert _interrupt_value(interrupted) is not None
    resumed = await graph.ainvoke(Command(resume={"action": "request_rework"}), config=config)
    assert resumed["human_action"] == "request_rework"
    assert resumed["rounds_used"] == 1
    inbox = resumed["case_review_inbox"]
    current = inbox["current_trigger"]
    assert current is not None
    assert current["predecessor"] == "review-round-advance"
    assert current["value"] == {"rounds_used": 1, "rounds_budget": 2}
    assert [call.semantic_node_id for call in harness._kernel.semantic_calls].count("intake.case-design") == 2
    assert resumed.get("decision") in {"pass", "approved"}


async def test_request_rework_validates_after_restart_and_advances_once() -> None:
    harness = GraphHarness()
    backend = harness.anchored_memory_checkpointer()
    await _prepare_anchored_backend(backend)
    harness.recording_context(owner_id="assurance.intake", contracts=_contracts())
    builder: StateGraph[IntakeState] = StateGraph(IntakeState)
    builder.add_node("human-review", cast(Callable[..., Any], human_review))
    builder.add_edge(START, "human-review")
    builder.add_edge("human-review", END)
    graph = builder.compile(checkpointer=backend)
    first = await graph.ainvoke(
        {"rounds_used": 0, "rounds_budget": 2, "decision": "needs_human_review"},
        config=_config(),
    )
    del first
    resumed = await graph.ainvoke(Command(resume={"action": "request_rework"}), config=_config())
    assert resumed["human_action"] == "request_rework"
    advanced = advance_review_round_node(resumed)
    assert advanced["rounds_used"] == 1
