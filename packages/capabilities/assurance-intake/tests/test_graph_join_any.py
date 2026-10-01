from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from graph_engine.attempts.contracts import TaskAttemptContract
from graph_engine.attempts.keys import BusinessActivation, derive_attempt_key
from graph_engine.attempts.resolutions import ReceiptRef
from graph_engine.testing import GraphHarness

from assurance_intake.contracts.workflow import CaseReworkContextV1, EvidenceArtifactRefV1, ReviewedCaseV1
from assurance_intake.feature import AGENT_JOB_CONTRACTS, TASK_ATTEMPT_CONTRACTS
from assurance_intake.graphs.calls import (
    activation_review_round,
    select_case_design,
    select_case_repair,
    select_case_review,
)
from assurance_intake.graphs.factory import build_intake_graphs
from assurance_intake.graphs.case import review_round_advance
from tests.architecture.loop_scc_inventory import LOOP_SCC_INVENTORY

_SHA = "a" * 64


def _plan_ref() -> dict[str, str]:
    return {
        "path": f"qa/results/plan/{_SHA}/resolved-assurance-plan.json",
        "digest": _SHA,
    }


def _base_state(**extra: object) -> dict[str, object]:
    state: dict[str, object] = {
        "change_id": "CH-DEMO-001",
        "plan_digest": _SHA,
        "plan_ref": _plan_ref(),
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
        "coverage_epoch": 0,
        "preparation_refs": [
            {"path": "qa/requirement.md", "digest": _SHA},
            _plan_ref(),
        ],
        "rounds_used": 0,
        "rounds_budget": 2,
    }
    state.update(extra)
    return state


def _reviewed(*, epoch: int = 0) -> ReviewedCaseV1:
    plan = EvidenceArtifactRefV1.model_validate(_plan_ref())
    return ReviewedCaseV1(
        change_id="CH-DEMO-001",
        coverage_epoch=epoch,
        plan_digest=_SHA,
        plan_ref=plan,
        preparation_refs=(
            EvidenceArtifactRefV1(path="qa/requirement.md", digest=_SHA),
            plan,
        ),
        case_refs=(EvidenceArtifactRefV1(path="qa/cases/menus/case.yaml", digest=_SHA),),
        review_ref=EvidenceArtifactRefV1(path="qa/results/review/case-review.json", digest=_SHA),
        selection_ref=EvidenceArtifactRefV1(
            path=f"qa/results/cases/epochs/{epoch}/selection.json",
            digest=_SHA,
        ),
    )


def _rework_state() -> dict[str, object]:
    previous = _reviewed()
    gap = EvidenceArtifactRefV1(path="qa/results/inspect/coverage-gaps.json", digest=_SHA)
    context = CaseReworkContextV1(
        previous_case=previous,
        inspect_receipt=ReceiptRef(receipt_id="inspect", receipt_digest=_SHA),
        assessment_refs=(gap,),
        gaps_ref=gap,
        target_case_paths=("qa/cases/menus/case.yaml",),
    )
    return _base_state(
        coverage_epoch=previous.coverage_epoch + 1,
        case_rework_context=context.model_dump(mode="json"),
        preparation_refs=[item.model_dump(mode="json") for item in previous.preparation_refs],
        ui_exploration_ref={"path": "qa/results/facts/ui-exploration.json", "digest": _SHA},
        api_discovery_ref={"path": "qa/results/facts/api-discovery.json", "digest": _SHA},
        rounds_used=1,
    )


def test_review_round_activation_follows_rounds_used() -> None:
    state = {"rounds_used": 0, "rounds_budget": 2}
    assert activation_review_round(state) == BusinessActivation.for_round(0)
    advanced = review_round_advance(state)
    assert advanced == {"rounds_used": 1, "rounds_budget": 2}
    assert activation_review_round({**state, **advanced}) == BusinessActivation.for_round(1)
    with pytest.raises(KeyError):
        activation_review_round({})
    with pytest.raises(TypeError, match="rounds_used"):
        activation_review_round({"rounds_used": True})
    with pytest.raises((ValidationError, ValueError)):
        review_round_advance({"rounds_used": 2, "rounds_budget": 2})


def _attempt_key(node: str, activation: BusinessActivation, validated: BaseModel) -> str:
    return derive_attempt_key(
        invocation_id="inv-1",
        graph_revision=_SHA,
        public_entrypoint="case",
        semantic_node_id=node,
        business_activation=activation,
        contract_id="assurance.intake.agent.case-design.v1",
        validated_input=validated,
    ).digest


def test_shared_round_activation_stays_unique_by_node_and_input() -> None:
    state = _base_state()
    design = select_case_design(state)
    review = select_case_review(state)
    repair = select_case_repair(state)
    round_zero = activation_review_round(state)
    round_one = activation_review_round(review_round_advance(state))
    design_key = _attempt_key("intake.case-design", round_zero, design)
    assert design_key != _attempt_key("intake.case-review", round_zero, review)
    assert design_key != _attempt_key("intake.case-repair", round_zero, repair)
    assert design_key != _attempt_key("intake.case-design", round_one, design)
    later = select_case_design({**state, "coverage_epoch": 1})
    assert design_key != _attempt_key("intake.case-design", round_zero, later)


def test_case_design_select_preserves_rework_epoch() -> None:
    state = _rework_state()
    selected = select_case_design(state)
    assert selected.coverage_epoch == 1
    assert selected.validation_error is None
    assert selected.case_rework_context is not None
    assert selected.case_rework_context.previous_case.coverage_epoch == 0
    assert [item.model_dump(mode="json") for item in selected.preparation_refs] == state["preparation_refs"]
    assert selected.ui_exploration_ref is not None
    assert selected.ui_exploration_ref.path == "qa/results/facts/ui-exploration.json"
    assert selected.api_discovery_ref is not None
    assert selected.api_discovery_ref.path == "qa/results/facts/api-discovery.json"
    repair = select_case_repair(state)
    assert repair.preparation_refs == selected.preparation_refs
    assert repair.ui_exploration_ref == selected.ui_exploration_ref
    assert "coverage_epoch" not in repair.model_dump()
    with pytest.raises(ValidationError):
        select_case_design({**state, "coverage_epoch": 0})


def _contracts() -> dict[str, TaskAttemptContract[Any, Any]]:
    return {
        **{contract.contract_id: contract.to_task_contract() for contract in AGENT_JOB_CONTRACTS.values()},
        **{contract.contract_id: contract for contract in TASK_ATTEMPT_CONTRACTS.values()},
    }


def _cycle_members(edges: set[tuple[str, str]]) -> set[str]:
    nodes = {node for edge in edges for node in edge if node not in {"__start__", "__end__"}}

    def reachable(start: str) -> set[str]:
        seen: set[str] = set()
        stack = [start]
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            stack.extend(
                target
                for source, target in edges
                if source == current and target not in seen and target not in {"__start__", "__end__"}
            )
        return seen

    forward = {node: reachable(node) for node in nodes}
    return {
        node
        for node in nodes
        if any(other != node and node in forward[other] and other in forward[node] for other in nodes)
    }


def test_case_loop_reenters_through_review_round_advance() -> None:
    harness = GraphHarness()
    bundle = build_intake_graphs(
        harness.recording_context(owner_id="assurance.intake", contracts=_contracts())
    )
    assert "advance-join" not in bundle.case.nodes
    edges = {(str(edge.source), str(edge.target)) for edge in bundle.case.get_graph().edges}
    assert ("review-round-advance", "case-design") in edges
    assert ("review-round-advance", "case-repair") in edges
    assert ("case-review", "review-round-advance") in edges
    assert ("human-review", "review-round-advance") in edges
    assert ("case-design", "case-review") in edges
    assert ("case-design", "failed") in edges
    assert ("case-repair", "exhausted") in edges
    row = next(
        item for item in LOOP_SCC_INVENTORY if item.graph_id == "assurance.intake.workflow.graph.entry"
    )
    assert row.anchor_node_id == "review-round-advance"
    assert set(row.membership) == _cycle_members(edges)
