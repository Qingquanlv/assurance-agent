from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest
from langchain_core.runnables.config import RunnableConfig
from langgraph.errors import GraphInterrupt
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command
from pydantic import ValidationError

from agent_runtime_contracts.ops import ArtifactListResultV1
from assurance_intake.feature import AGENT_JOB_CONTRACTS, TASK_ATTEMPT_CONTRACTS
from assurance_intake.contracts.review import CaseReviewResultV1
from assurance_intake.graphs.calls import publish_case_review
from assurance_intake.graphs.case import (
    HUMAN_REVIEW_ACTIONS,
    ReviewRoundAdvanceOutput,
    advance_review_round,
    human_review,
    review_round_advance,
    route_human_review,
    terminal_rejected,
    terminal_reviewed,
)
from assurance_intake.graphs.factory import build_intake_graphs
from assurance_intake.graphs.state import IntakeState
from graph_engine.attempts.contracts import TaskAttemptContract
from graph_engine.attempts.resolutions import ReceiptRef
from graph_engine.testing import GraphHarness, committed
from graph_engine.testing.graph_harness import _prepare_anchored_backend

_SHA = "a" * 64
_RECEIPT = ReceiptRef(receipt_id="receipt-1", receipt_digest=_SHA)


def _contracts() -> dict[str, TaskAttemptContract[Any, Any]]:
    return {
        **{contract.contract_id: contract.to_task_contract() for contract in AGENT_JOB_CONTRACTS.values()},
        **{contract.contract_id: contract for contract in TASK_ATTEMPT_CONTRACTS.values()},
    }


def _input() -> dict[str, object]:
    return {
        "change_id": "CH-DEMO-001",
        "requirement": "Cover department CRUD.",
        "plan_digest": _SHA,
        "plan_ref": {
            "path": f"qa/results/plan/{_SHA}/resolved-assurance-plan.json",
            "digest": _SHA,
        },
        "selected_test_families": ["api"],
        "case_delta_paths": ["qa/cases/menus/case.yaml"],
        "capability_leafs": ["entities.item.create"],
        "allowed_artifact_paths": [
            "qa/.qa.yaml",
            "qa/cases",
            "qa/fixtures",
            "qa/proposal.md",
            "qa/requirement.md",
            "qa/results",
            "qa/tests",
        ],
        "rounds_used": 0,
        "rounds_budget": 2,
        "coverage_epoch": 0,
        "preparation_refs": [
            {"path": "qa/requirement.md", "digest": _SHA},
            {
                "path": f"qa/results/plan/{_SHA}/resolved-assurance-plan.json",
                "digest": _SHA,
            },
        ],
    }


def _artifact() -> ArtifactListResultV1:
    return ArtifactListResultV1(output_files=("qa/proposal.md",))


def _design() -> dict[str, object]:
    return {
        "output_files": ["qa/proposal.md"],
        "artifacts": [
            {
                "path": "qa/cases/menus/case.yaml",
                "digest": _SHA,
            }
        ],
    }


def _repair() -> dict[str, object]:
    return {
        "artifacts": [
            {
                "path": "qa/cases/menus/case.yaml",
                "digest": _SHA,
            }
        ],
    }


def _reviewed_case() -> dict[str, object]:
    plan_ref = {
        "path": f"qa/results/plan/{_SHA}/resolved-assurance-plan.json",
        "digest": _SHA,
    }
    return {
        "change_id": "CH-DEMO-001",
        "coverage_epoch": 0,
        "plan_digest": _SHA,
        "plan_ref": plan_ref,
        "preparation_refs": [
            {"path": "qa/requirement.md", "digest": _SHA},
            plan_ref,
        ],
        "case_refs": [{"path": "qa/cases/menus/case.yaml", "digest": _SHA}],
        "review_ref": {"path": "qa/results/review/case-review.json", "digest": _SHA},
        "selection_ref": {"path": "qa/results/cases/epochs/0/selection.json", "digest": _SHA},
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
        "artifacts": [
            {
                "path": "qa/results/review/case-review.json",
                "digest": _SHA,
            },
            {
                "path": "qa/results/cases/epochs/0/selection.json",
                "digest": _SHA,
            },
        ],
        "reviewed_case": _reviewed_case(),
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
            "artifacts": [
                {
                    "path": "qa/results/review/case-review.json",
                    "digest": _SHA,
                },
                {
                    "path": "qa/results/cases/epochs/0/selection.json",
                    "digest": _SHA,
                },
            ],
            "reviewed_case": _reviewed_case(),
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


def test_publish_case_review_ignores_agent_authored_rounds() -> None:
    state = {"rounds_used": 0, "rounds_budget": 2}
    result = _review_result_v1("needs_fix", auto_fix=True)
    dumped = result.model_dump(mode="json")
    assert "rounds_used" not in dumped
    assert "rounds_budget" not in dumped
    published = publish_case_review(state, result, None)
    assert "rounds_used" not in published
    assert "rounds_budget" not in published
    authored = {**dumped, "rounds_used": 1, "rounds_budget": 3}
    published_authored = publish_case_review(state, authored, None)
    assert "rounds_used" not in published_authored
    assert "rounds_budget" not in published_authored


def _patch_interrupt(node: Callable[..., Any], **kwargs: Any):
    return patch.dict(node.__globals__, {"interrupt": MagicMock(**kwargs)})


def test_interrupt_accepts_only_approve_reject_request_rework() -> None:
    assert HUMAN_REVIEW_ACTIONS == ("approve", "reject", "request_rework")
    with _patch_interrupt(human_review, return_value={"action": "supersede"}):
        with pytest.raises(ValidationError):
            human_review({"rounds_used": 0, "rounds_budget": 2})
    with _patch_interrupt(human_review, return_value={"action": "hold"}):
        with pytest.raises(ValidationError):
            human_review({"rounds_used": 0, "rounds_budget": 2})


def test_interrupt_node_validates_after_restart_and_does_not_mutate_before_interrupt() -> None:
    seen: list[object] = []

    def _first(payload: object) -> object:
        seen.append(payload)
        raise RuntimeError("interrupt")

    with _patch_interrupt(human_review, side_effect=_first):
        with pytest.raises(RuntimeError, match="interrupt"):
            human_review({"rounds_used": 0, "rounds_budget": 2, "decision": "needs_human_review"})
    assert seen
    request = seen[0]
    assert isinstance(request, dict)
    assert set(request["actions"]) == {"approve", "reject", "request_rework"}

    with _patch_interrupt(human_review, return_value={"action": "approve"}):
        update = human_review({"rounds_used": 0, "rounds_budget": 2, "decision": "needs_human_review"})
    assert update == {"human_action": "approve"}
    assert "decision" not in update
    assert "rounds_used" not in update


def test_review_round_advance_is_budget_arithmetic() -> None:
    payload = {"rounds_used": 0, "rounds_budget": 2}
    output = review_round_advance(payload)
    expected = advance_review_round(payload)
    assert isinstance(expected, ReviewRoundAdvanceOutput)
    assert output["rounds_used"] == expected.rounds_used == 1
    assert output["rounds_budget"] == expected.rounds_budget == 2
    with pytest.raises((ValidationError, ValueError)):
        review_round_advance({"rounds_used": 2, "rounds_budget": 2})


def test_needs_human_review_approve_reaches_reviewed_terminal() -> None:
    state = {"rounds_used": 0, "rounds_budget": 2, "reviewed_case": {"stale": True}, "case_receipt": None}
    published = publish_case_review(state, _review("needs_human_review", human=True), _RECEIPT)
    assert published["reviewed_case"] == _reviewed_case()
    assert published["case_receipt"] == _RECEIPT.model_dump(mode="json")
    merged = {**state, **published, "human_action": "approve"}
    assert route_human_review(merged) == "done"
    terminal = terminal_reviewed(merged)
    assert terminal["status"] == "reviewed"
    assert terminal["reviewed_case"] == _reviewed_case()
    assert terminal["receipt"] == _RECEIPT.model_dump(mode="json")


def test_human_reject_clears_reviewed_evidence() -> None:
    state = {"rounds_used": 0, "rounds_budget": 2}
    published = publish_case_review(state, _review("needs_human_review", human=True), _RECEIPT)
    assert published["reviewed_case"] is not None
    rejected = terminal_rejected({**state, **published, "human_action": "reject"})
    assert route_human_review({**state, **published, "human_action": "reject"}) == "rejected"
    assert rejected["reviewed_case"] is None
    assert rejected["case_receipt"] is None
    dropped = publish_case_review(state, _review("reject"), _RECEIPT)
    assert dropped["reviewed_case"] is None
    assert dropped["case_receipt"] is None


async def test_pass_completes_without_advance() -> None:
    harness = GraphHarness()
    bundle = build_intake_graphs(
        harness.recording_context(owner_id="assurance.intake", contracts=_contracts())
    )
    result = await harness.run(
        bundle.case,
        input=_input(),
        script={
            "intake.intake": [committed(_artifact(), _RECEIPT)],
            "intake.explore": [committed(_artifact(), _RECEIPT)],
            "intake.case-design": [committed(_design(), _RECEIPT)],
            "intake.case-review": [committed(_review("pass"), _RECEIPT)],
        },
    )
    assert result.terminal is not None
    assert cast(dict[str, object], result.terminal).get("decision") == "pass"
    assert cast(dict[str, object], result.terminal).get("rounds_used") == 0


async def test_automatic_fix_advances_exactly_once() -> None:
    harness = GraphHarness()
    bundle = build_intake_graphs(
        harness.recording_context(owner_id="assurance.intake", contracts=_contracts())
    )
    result = await harness.run(
        bundle.case,
        input=_input(),
        script={
            "intake.intake": [committed(_artifact(), _RECEIPT)],
            "intake.explore": [committed(_artifact(), _RECEIPT)],
            "intake.case-design": [committed(_design(), _RECEIPT)],
            "intake.case-repair": [committed(_repair(), _RECEIPT)],
            "intake.case-review": [
                committed(_review("needs_fix", auto_fix=True, used=0), _RECEIPT),
                committed(_review("pass", used=1), _RECEIPT),
            ],
        },
    )
    terminal = cast(dict[str, object], result.terminal)
    assert terminal.get("rounds_used") == 1
    assert terminal.get("decision") == "pass"
    assert [call.semantic_node_id for call in result.semantic_calls] == [
        "intake.case-design",
        "intake.case-review",
        "intake.case-repair",
        "intake.case-review",
    ]


async def test_automatic_fix_advances_when_review_result_nulls_rounds() -> None:
    harness = GraphHarness()
    bundle = build_intake_graphs(
        harness.recording_context(owner_id="assurance.intake", contracts=_contracts())
    )
    result = await harness.run(
        bundle.case,
        input=_input(),
        script={
            "intake.intake": [committed(_artifact(), _RECEIPT)],
            "intake.explore": [committed(_artifact(), _RECEIPT)],
            "intake.case-design": [committed(_design(), _RECEIPT)],
            "intake.case-repair": [committed(_repair(), _RECEIPT)],
            "intake.case-review": [
                committed(_review_result_v1("needs_fix", auto_fix=True), _RECEIPT),
                committed(_review_result_v1("pass"), _RECEIPT),
            ],
        },
    )
    terminal = cast(dict[str, object], result.terminal)
    assert terminal.get("rounds_used") == 1
    assert terminal.get("decision") == "pass"
    calls = [call.semantic_node_id for call in result.semantic_calls]
    assert calls.count("intake.case-design") == 1
    assert calls.count("intake.case-repair") == 1


async def test_reject_is_explicit_terminal() -> None:
    harness = GraphHarness()
    bundle = build_intake_graphs(
        harness.recording_context(owner_id="assurance.intake", contracts=_contracts())
    )
    result = await harness.run(
        bundle.case,
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
    assert terminal.get("reviewed_case") is None
    assert terminal.get("case_receipt") is None


async def test_human_approve_completes_reviewed_terminal() -> None:
    harness = GraphHarness()
    backend = harness.anchored_memory_checkpointer()
    await _prepare_anchored_backend(backend)
    bundle = build_intake_graphs(
        harness.recording_context(owner_id="assurance.intake", contracts=_contracts())
    )
    harness._kernel.load_script(
        {
            "intake.case-design": [committed(_design(), _RECEIPT)],
            "intake.case-review": [committed(_review("needs_human_review", human=True), _RECEIPT)],
        }
    )
    wrapper: StateGraph[IntakeState] = StateGraph(IntakeState)
    wrapper.add_node("case", bundle.case)
    wrapper.add_edge(START, "case")
    wrapper.add_edge("case", END)
    graph = wrapper.compile(checkpointer=backend)
    config = _config()
    interrupted: object | None
    try:
        interrupted = await graph.ainvoke(cast(Any, _input()), config=config)
    except GraphInterrupt as error:
        interrupted = error
    else:
        assert _interrupt_value(interrupted) is not None
    resumed = await graph.ainvoke(Command(resume={"action": "approve"}), config=config)
    assert resumed["status"] == "reviewed"
    assert resumed["reviewed_case"] == _reviewed_case()
    assert resumed["receipt"] == _RECEIPT.model_dump(mode="json")
    assert resumed["human_action"] == "approve"


async def test_budget_exhaustion_is_explicit_after_two_advances() -> None:
    harness = GraphHarness()
    bundle = build_intake_graphs(
        harness.recording_context(owner_id="assurance.intake", contracts=_contracts())
    )
    result = await harness.run(
        bundle.case,
        input=_input(),
        script={
            "intake.intake": [committed(_artifact(), _RECEIPT)],
            "intake.explore": [committed(_artifact(), _RECEIPT)],
            "intake.case-design": [committed(_design(), _RECEIPT)],
            "intake.case-repair": [committed(_repair(), _RECEIPT), committed(_repair(), _RECEIPT)],
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
    calls = [call.semantic_node_id for call in result.semantic_calls]
    assert calls.count("intake.case-design") == 1
    assert calls.count("intake.case-repair") == 2


def _interrupt_value(result: object) -> object | None:
    if not isinstance(result, dict):
        return None
    interrupts = result.get("__interrupt__")
    if not interrupts:
        return None
    first = interrupts[0]
    return getattr(first, "value", first)


async def test_request_rework_on_case_graph_advances_once_through_inbox() -> None:
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
    wrapper.add_node("case", bundle.case)
    wrapper.add_edge(START, "case")
    wrapper.add_edge("case", END)
    graph = wrapper.compile(checkpointer=backend)
    config = _config()
    interrupted: object | None
    try:
        interrupted = await graph.ainvoke(cast(Any, _input()), config=config)
    except GraphInterrupt as error:
        interrupted = error
    else:
        assert _interrupt_value(interrupted) is not None
    resumed = await graph.ainvoke(Command(resume={"action": "request_rework"}), config=config)
    assert resumed["human_action"] == "request_rework"
    assert resumed["rounds_used"] == 1
    assert "case_review_inbox" not in resumed
    calls = [call.semantic_node_id for call in harness._kernel.semantic_calls]
    assert calls.count("intake.case-design") == 2
    assert calls.count("intake.case-repair") == 0
    assert resumed.get("decision") == "pass"


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
    wrapper.add_node("case", bundle.case)
    wrapper.add_edge(START, "case")
    wrapper.add_edge("case", END)
    graph = wrapper.compile(checkpointer=backend)
    config = _config()
    interrupted: object | None
    try:
        interrupted = await graph.ainvoke(cast(Any, _input()), config=config)
    except GraphInterrupt as error:
        interrupted = error
    else:
        assert _interrupt_value(interrupted) is not None
    resumed = await graph.ainvoke(Command(resume={"action": "request_rework"}), config=config)
    assert resumed["human_action"] == "request_rework"
    assert resumed["rounds_used"] == 1
    assert "case_review_inbox" not in resumed
    calls = [call.semantic_node_id for call in harness._kernel.semantic_calls]
    assert calls.count("intake.case-design") == 2
    assert calls.count("intake.case-repair") == 0
    assert resumed.get("decision") == "pass"


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
    advanced = review_round_advance(resumed)
    assert advanced["rounds_used"] == 1
