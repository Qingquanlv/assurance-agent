from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest
from langchain_core.runnables.config import RunnableConfig
from langgraph.errors import GraphInterrupt
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command
from pydantic import ValidationError

from assurance_generation.contracts.attempts import AGENT_JOB_CONTRACTS, TASK_ATTEMPT_CONTRACTS
from assurance_generation.contracts.decisions import GenerationReviewRoundAdvanceOutput, advance_review_round
from assurance_generation.graphs.factory import build_generation_graphs
from assurance_generation.graphs.nodes import (
    HUMAN_REVIEW_ACTIONS,
    advance_review_round_node,
    human_review,
    human_review_retry,
    publish_plan_review,
)
from assurance_generation.graphs.state import GenerationState
from graph_engine.attempts.contracts import TaskAttemptContract
from graph_engine.attempts.resolutions import AttemptResolution, ReceiptRef
from graph_engine.testing import GraphHarness, committed
from graph_engine.testing.graph_harness import _prepare_anchored_backend

_SHA = "a" * 64
_RECEIPT = ReceiptRef(receipt_id="receipt-1", receipt_digest=_SHA)
_FAMILIES = ("api", "e2e", "fuzz", "performance")


def _contracts() -> dict[str, TaskAttemptContract[Any, Any]]:
    contracts = {
        contract.contract_id: contract.to_task_contract() for contract in AGENT_JOB_CONTRACTS.values()
    }
    contracts.update({contract.contract_id: contract for contract in TASK_ATTEMPT_CONTRACTS.values()})
    return contracts


def _input(family: str) -> dict[str, object]:
    return {
        "change_id": "CH-DEMO-001",
        "plan_digest": _SHA,
        "plan_ref": {
            "path": f"qa/results/plan/{_SHA}/resolved-assurance-plan.json",
            "digest": _SHA,
        },
        "selected_test_families": [family],
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
        "family": family,
        "lane_selected": True,
        "rounds_used": 0,
        "rounds_budget": 2,
        "review_stage": "codegen",
    }


def _plan() -> dict[str, object]:
    return {"artifacts": [{"path": "qa/results", "digest": _SHA}]}


def _review(
    route: str,
    *,
    used: int | None = 0,
    budget: int | None = 2,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "route": route,
        "finding_ids": [],
        "artifacts": [{"path": "qa/results", "digest": _SHA}],
    }
    if used is not None:
        payload["rounds_used"] = used
    if budget is not None:
        payload["rounds_budget"] = budget
    return payload


def _codegen(family: str) -> dict[str, object]:
    if family in {"api", "e2e"}:
        return {"schema_version": "2", "verdict": "accepted"}
    return {"schema_version": "1", "needs_fix": False}


def _reviewed_case() -> dict[str, object]:
    return {
        "change_id": "CH-DEMO-001",
        "coverage_epoch": 0,
        "preparation_refs": [{"path": "qa/requirement.md", "digest": _SHA}],
        "case_refs": [
            {
                "path": "qa/cases/menus/case.yaml",
                "digest": _SHA,
            }
        ],
        "review_ref": {
            "path": "qa/results/review/case-review.json",
            "digest": _SHA,
        },
        "selection_ref": {
            "path": "qa/results/cases/epochs/0/selection.json",
            "digest": _SHA,
        },
    }


def _family_graph(bundle: object, family: str) -> object:
    return getattr(bundle, family)


def _config(family: str) -> RunnableConfig:
    del family
    return {
        "configurable": {
            "thread_id": "inv-1",
            "assurance_revision_id": "a" * 64,
            "assurance_product_lock_digest": "b" * 64,
            "assurance_root_input_digest": "c" * 64,
            "assurance_fencing_token": 1,
            "assurance_entrypoint": "generate",
        }
    }


def test_publish_plan_review_keeps_graph_rounds_when_result_omits_or_nulls_them() -> None:
    state = {"rounds_used": 0, "rounds_budget": 2}
    dumped = _review("auto_fix")
    dumped["rounds_used"] = None
    dumped["rounds_budget"] = None
    published = publish_plan_review(state, dumped, None)
    assert published["rounds_used"] == 0
    assert published["rounds_budget"] == 2
    omitted = {key: value for key, value in dumped.items() if key not in {"rounds_used", "rounds_budget"}}
    published_omitted = publish_plan_review(state, omitted, None)
    assert published_omitted["rounds_used"] == 0
    assert published_omitted["rounds_budget"] == 2
    authored = {**dumped, "rounds_used": 1, "rounds_budget": 3}
    published_authored = publish_plan_review(state, authored, None)
    assert published_authored["rounds_used"] == 1
    assert published_authored["rounds_budget"] == 3


def _patch_interrupt(node: Callable[..., Any], **kwargs: Any):
    return patch.dict(node.__globals__, {"interrupt": MagicMock(**kwargs)})


@pytest.mark.parametrize("family", _FAMILIES)
def test_both_interrupt_sites_accept_only_approve_reject_request_rework(family: str) -> None:
    assert HUMAN_REVIEW_ACTIONS == ("approve", "reject", "request_rework")
    state = {"family": family, "rounds_used": 0, "rounds_budget": 2}
    for node in (human_review, human_review_retry):
        with _patch_interrupt(node, return_value={"action": "supersede"}):
            with pytest.raises(ValidationError):
                node(state)
        with _patch_interrupt(node, return_value={"action": "hold"}):
            with pytest.raises(ValidationError):
                node(state)


@pytest.mark.parametrize("family", _FAMILIES)
def test_interrupt_node_validates_after_restart_and_does_not_mutate_before_interrupt(family: str) -> None:
    seen: list[object] = []

    def _first(payload: object) -> object:
        seen.append(payload)
        raise RuntimeError("interrupt")

    state = {"family": family, "rounds_used": 0, "rounds_budget": 2, "decision": "needs_human_review"}
    with _patch_interrupt(human_review, side_effect=_first):
        with pytest.raises(RuntimeError, match="interrupt"):
            human_review(state)
    assert seen
    request = seen[0]
    assert isinstance(request, dict)
    assert set(request["actions"]) == {"approve", "reject", "request_rework"}
    assert request["interrupt_id"] == f"{family}-codegen-human-review"
    assert request["ordinal"] == 0
    assert request["reason"] == f"{family}_codegen_needs_human_review"

    with _patch_interrupt(human_review, return_value={"action": "approve"}):
        update = human_review(state)
    assert update == {"human_action": "approve"}
    assert "decision" not in update
    assert "rounds_used" not in update

    retry_seen: list[object] = []

    def _retry(payload: object) -> object:
        retry_seen.append(payload)
        raise RuntimeError("interrupt")

    with _patch_interrupt(human_review_retry, side_effect=_retry):
        with pytest.raises(RuntimeError, match="interrupt"):
            human_review_retry(state)
    retry_request = retry_seen[0]
    assert isinstance(retry_request, dict)
    assert retry_request["interrupt_id"] == f"{family}-codegen-human-review-retry"
    assert retry_request["ordinal"] == 1
    assert retry_request["reason"] == f"{family}_codegen_needs_human_review"


def test_advance_review_round_node_is_the_moved_pure_function() -> None:
    payload = {"family": "api", "review_stage": "codegen", "rounds_used": 0, "rounds_budget": 2}
    output = advance_review_round_node(payload)
    expected = advance_review_round(
        {"family": "api", "stage": "codegen", "rounds_used": 0, "rounds_budget": 2}
    )
    assert isinstance(expected, GenerationReviewRoundAdvanceOutput)
    assert output["rounds_used"] == expected.rounds_used == 1
    assert output["rounds_budget"] == expected.rounds_budget == 2
    with pytest.raises((ValidationError, ValueError)):
        advance_review_round_node(
            {"family": "api", "review_stage": "codegen", "rounds_used": 2, "rounds_budget": 2}
        )


@pytest.mark.parametrize("family", _FAMILIES)
async def test_pass_completes_without_advance(family: str) -> None:
    harness = GraphHarness()
    bundle = build_generation_graphs(
        harness.recording_context(owner_id="assurance.generation", contracts=_contracts())
    )
    result = await harness.run(
        _family_graph(bundle, family),
        input=_input(family),
        script={
            f"generation.{family}.codegen": [committed(_codegen(family), _RECEIPT)],
            f"generation.{family}.codegen-review": [committed(_review("codegen"), _RECEIPT)],
        },
    )
    terminal = cast(dict[str, object], result.terminal)
    assert terminal.get("rounds_used") == 0
    assert terminal.get("status") in {"passed", "done"} or terminal.get("decision") == "pass"


@pytest.mark.parametrize("family", _FAMILIES)
async def test_automatic_fix_advances_exactly_once(family: str) -> None:
    harness = GraphHarness()
    bundle = build_generation_graphs(
        harness.recording_context(owner_id="assurance.generation", contracts=_contracts())
    )
    result = await harness.run(
        _family_graph(bundle, family),
        input=_input(family),
        script={
            f"generation.{family}.codegen": [
                committed(_codegen(family), _RECEIPT),
                committed(_codegen(family), _RECEIPT),
            ],
            f"generation.{family}.codegen-review": [
                committed(_review("auto_fix", used=0), _RECEIPT),
                committed(_review("codegen", used=1), _RECEIPT),
            ],
        },
    )
    terminal = cast(dict[str, object], result.terminal)
    assert terminal.get("rounds_used") == 1
    assert [call.semantic_node_id for call in result.semantic_calls].count(
        f"generation.{family}.codegen"
    ) == 2


@pytest.mark.parametrize("family", _FAMILIES)
async def test_automatic_fix_advances_when_review_result_nulls_rounds(family: str) -> None:
    harness = GraphHarness()
    bundle = build_generation_graphs(
        harness.recording_context(owner_id="assurance.generation", contracts=_contracts())
    )
    result = await harness.run(
        _family_graph(bundle, family),
        input=_input(family),
        script={
            f"generation.{family}.codegen": [
                committed(_codegen(family), _RECEIPT),
                committed(_codegen(family), _RECEIPT),
            ],
            f"generation.{family}.codegen-review": [
                committed(_review("auto_fix", used=None, budget=None), _RECEIPT),
                committed(_review("codegen", used=None, budget=None), _RECEIPT),
            ],
        },
    )
    terminal = cast(dict[str, object], result.terminal)
    assert terminal.get("rounds_used") == 1


@pytest.mark.parametrize("family", _FAMILIES)
async def test_reject_is_explicit_terminal(family: str) -> None:
    harness = GraphHarness()
    bundle = build_generation_graphs(
        harness.recording_context(owner_id="assurance.generation", contracts=_contracts())
    )
    result = await harness.run(
        _family_graph(bundle, family),
        input=_input(family),
        script={
            f"generation.{family}.codegen": [committed(_codegen(family), _RECEIPT)],
            f"generation.{family}.codegen-review": [committed(_review("reject"), _RECEIPT)],
        },
    )
    terminal = cast(dict[str, object], result.terminal)
    assert terminal.get("decision") == "reject" or terminal.get("status") == "rejected"


@pytest.mark.parametrize("family", _FAMILIES)
async def test_budget_exhaustion_is_explicit_after_two_advances(family: str) -> None:
    harness = GraphHarness()
    bundle = build_generation_graphs(
        harness.recording_context(owner_id="assurance.generation", contracts=_contracts())
    )
    result = await harness.run(
        _family_graph(bundle, family),
        input=_input(family),
        script={
            f"generation.{family}.codegen": [
                committed(_codegen(family), _RECEIPT),
                committed(_codegen(family), _RECEIPT),
                committed(_codegen(family), _RECEIPT),
            ],
            f"generation.{family}.codegen-review": [
                committed(_review("auto_fix", used=0), _RECEIPT),
                committed(_review("auto_fix", used=1), _RECEIPT),
                committed(_review("auto_fix", used=2), _RECEIPT),
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


@pytest.mark.parametrize("family", _FAMILIES)
@pytest.mark.parametrize("action", ("approve", "reject", "request_rework"))
async def test_human_action_on_family_graph(family: str, action: str) -> None:
    harness = GraphHarness()
    backend = harness.anchored_memory_checkpointer()
    await _prepare_anchored_backend(backend)
    bundle = build_generation_graphs(
        harness.recording_context(owner_id="assurance.generation", contracts=_contracts())
    )
    reviews = (
        [_review("human"), _review("codegen", used=1)] if action == "request_rework" else [_review("human")]
    )
    script: dict[str, list[AttemptResolution]] = {
        f"generation.{family}.codegen": [committed(_codegen(family), _RECEIPT)],
        f"generation.{family}.codegen-review": [committed(item, _RECEIPT) for item in reviews],
    }
    if action == "request_rework":
        script[f"generation.{family}.codegen"].append(committed(_codegen(family), _RECEIPT))
    harness._kernel.load_script(script)
    wrapper: StateGraph[GenerationState] = StateGraph(GenerationState)
    wrapper.add_node("family", cast(Any, _family_graph(bundle, family)))
    wrapper.add_edge(START, "family")
    wrapper.add_edge("family", END)
    graph = wrapper.compile(checkpointer=backend)
    config = _config(family)
    interrupted: object | None
    try:
        interrupted = await graph.ainvoke(cast(Any, _input(family)), config=config)
    except GraphInterrupt as error:
        interrupted = error
    else:
        assert _interrupt_value(interrupted) is not None
    value = _interrupt_value(interrupted)
    if isinstance(value, dict):
        assert value.get("interrupt_id") == f"{family}-codegen-human-review"
        assert value.get("ordinal") == 0
    resumed = await graph.ainvoke(Command(resume={"action": action}), config=config)
    assert resumed["human_action"] == action
    if action == "request_rework":
        assert resumed["rounds_used"] == 1
        inbox = resumed["plan_round_inbox"]
        current = inbox["current_trigger"]
        assert current is not None
        assert current["predecessor"] == "codegen-review-round-advance"
        assert current["value"] == {"rounds_used": 1, "rounds_budget": 2}
        assert resumed["current_trigger"] == current
        assert [call.semantic_node_id for call in harness._kernel.semantic_calls].count(
            f"generation.{family}.codegen"
        ) == 2
    elif action == "approve":
        assert resumed.get("status") in {"passed", "done"} or resumed.get("decision") == "pass"
    else:
        assert resumed.get("decision") == "reject" or resumed.get("status") == "rejected"


async def test_root_fanout_surfaces_resumable_family_human_interrupt(tmp_path: Path) -> None:
    from assurance_generation.operations.cycle import complete_generation_cycle
    from test_generation_cycle import cycle_fixture  # pyright: ignore[reportMissingImports]

    cycle_input, script = await cycle_fixture(tmp_path, ("api",))
    cycle_result = complete_generation_cycle(cycle_input, tmp_path, tmp_path / ".stage")
    harness = GraphHarness()
    backend = harness.anchored_memory_checkpointer()
    await _prepare_anchored_backend(backend)
    bundle = build_generation_graphs(
        harness.recording_context(owner_id="assurance.generation", contracts=_contracts())
    )
    script["generation.api.codegen-review"] = [committed(_review("human"), _RECEIPT)]
    script["generation.publish-cycle"] = [committed(cycle_result.model_dump(mode="json"), _RECEIPT)]
    harness._kernel.load_script(script)
    wrapper: StateGraph[GenerationState] = StateGraph(GenerationState)
    wrapper.add_node("generation", cast(Any, bundle.generation))
    wrapper.add_edge(START, "generation")
    wrapper.add_edge("generation", END)
    graph = wrapper.compile(checkpointer=backend)
    config = _config("api")
    payload = {
        "change_id": "CH-DEMO-001",
        "plan_digest": cycle_input.plan_digest,
        "plan_ref": cycle_input.plan_ref.model_dump(mode="json"),
        "selected_test_families": ["api"],
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
        "reviewed_case": cycle_input.reviewed_case.model_dump(mode="json"),
    }
    interrupted: object | None
    try:
        interrupted = await graph.ainvoke(cast(Any, payload), config=config)
    except GraphInterrupt as error:
        interrupted = error
    else:
        assert _interrupt_value(interrupted) is not None
    value = _interrupt_value(interrupted)
    assert isinstance(value, dict)
    assert value.get("interrupt_id") == "api-codegen-human-review"
    assert value.get("ordinal") == 0
    resumed = await graph.ainvoke(Command(resume={"action": "approve"}), config=config)
    assert _interrupt_value(resumed) is None
    results = resumed.get("family_results") or []
    by_family = {item["family"]: item for item in results if isinstance(item, dict)}
    assert by_family["api"]["status"] == "passed"
    assert by_family["e2e"]["status"] == "skipped"
    assert by_family["fuzz"]["status"] == "skipped"
    assert by_family["performance"]["status"] == "skipped"


async def test_request_rework_validates_after_restart_and_advances_once() -> None:
    harness = GraphHarness()
    backend = harness.anchored_memory_checkpointer()
    await _prepare_anchored_backend(backend)
    harness.recording_context(owner_id="assurance.generation", contracts=_contracts())
    builder: StateGraph[GenerationState] = StateGraph(GenerationState)
    builder.add_node("codegen-human-review", cast(Callable[..., Any], human_review))
    builder.add_edge(START, "codegen-human-review")
    builder.add_edge("codegen-human-review", END)
    graph = builder.compile(checkpointer=backend)
    await graph.ainvoke(
        cast(
            Any,
            {
                "family": "api",
                "rounds_used": 0,
                "rounds_budget": 2,
                "decision": "needs_human_review",
                "review_stage": "codegen",
            },
        ),
        config=_config("api"),
    )
    resumed = await graph.ainvoke(Command(resume={"action": "request_rework"}), config=_config("api"))
    assert resumed["human_action"] == "request_rework"
    advanced = advance_review_round_node(resumed)
    assert advanced["rounds_used"] == 1
