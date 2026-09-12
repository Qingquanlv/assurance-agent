from __future__ import annotations

import inspect
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from graph_engine import ENGINE_API_VERSION, RegistryPorts
from graph_engine.plugin_api import AttemptContractRef, TaskContext

from agent_runtime_contracts import AgentExecutionContract
from assurance_generation.contracts.agent import CodegenInputV1, PlanInputV1
from assurance_generation.contracts.attempts import (
    AGENT_JOB_CONTRACTS,
    TASK_ATTEMPT_CONTRACTS,
    attempt_contract_refs,
)
from assurance_generation.contracts.codegen import (
    CodegenAuthoringV1,
    CodegenResultV1,
)
from assurance_generation.contracts.decisions import (
    GenerationCompletionOutput,
    GenerationReviewRoundAdvanceOutput,
    advance_review_round,
    complete_generation,
)
from assurance_generation.contracts.plans import PlanResultV1
from assurance_generation.contracts.reviews import PlanReview, PlanReviewAuthoring
from assurance_generation.operations.workflow_state import (
    GenerationCompleteHandler,
    GenerationReviewRoundAdvanceHandler,
)
from assurance_generation.plugin import GenerationPlugin
from tests.product.test_change_local_output_routing import execute_task

_FAMILIES = ("api", "e2e", "fuzz", "performance")
_PLAN_REVIEW_PROFILE = "assurance-v1-reviewer"
_PLAN_PROFILE = "assurance-v1-doc-author"
_CODEGEN_PROFILE = "assurance-v1-test-author"


def test_generation_round_history_routes_include_epoch_and_local_round() -> None:
    from assurance_generation.contracts.attempts import OUTPUT_ROUTE_TEMPLATES

    plan_pattern = "qa/results/plan/api/reviews/epochs/{coverage_epoch}/rounds/{review_round}.json"
    assert plan_pattern in OUTPUT_ROUTE_TEMPLATES["api.plan-review"]
    assert plan_pattern.format(coverage_epoch=0, review_round=0) != plan_pattern.format(
        coverage_epoch=1, review_round=0
    )


def test_generation_owns_twelve_agent_contracts_and_two_tasks() -> None:
    assert len(AGENT_JOB_CONTRACTS) == 12
    assert tuple(TASK_ATTEMPT_CONTRACTS) == (
        "resolve-inputs",
        "publish-cycle",
        "init-test-runtime",
    )
    resolver = TASK_ATTEMPT_CONTRACTS["resolve-inputs"]
    assert resolver.contract_id == "assurance.generation.resolve-inputs"
    assert resolver.handler_id == "assurance.generation.resolve-inputs.execute"
    assert resolver.owner_id == "assurance.generation"
    for contract in AGENT_JOB_CONTRACTS.values():
        assert isinstance(contract, AgentExecutionContract)
        assert contract.owner_id == "assurance.generation"
        assert contract.validators == ()
        assert contract.retry.max_attempts == 10
        assert contract.retry.interval_seconds == 10
        assert contract.timeout.seconds == 60
        claims = contract.phase_write_claims
        assert set(claims.prepare) | set(claims.runtime) | set(claims.finalize) <= set(
            contract.resources.writes
        )


def test_generation_agent_catalog_preserves_semantic_ids_and_models() -> None:
    expected_bases = (
        "api.codegen",
        "api.plan-review",
        "api.plan",
        "e2e.codegen",
        "e2e.plan-review",
        "e2e.plan",
        "fuzz.codegen",
        "fuzz.plan-review",
        "fuzz.plan",
        "performance.codegen",
        "performance.plan-review",
        "performance.plan",
    )
    assert tuple(sorted(AGENT_JOB_CONTRACTS)) == tuple(sorted(expected_bases))
    for base, contract in AGENT_JOB_CONTRACTS.items():
        family, _, stage = base.partition(".")
        assert contract.contract_id == f"assurance.generation.agent.{base}.v1"
        assert contract.prepare_handler_id == f"assurance.generation.{base}.prepare"
        assert contract.finalize_handler_id == f"assurance.generation.{base}.finalize"
        if stage == "plan":
            assert contract.input_model is PlanInputV1
            assert contract.agent_result_model is PlanResultV1
            assert contract.output_model is PlanResultV1
            assert contract.agent_profile == _PLAN_PROFILE
            assert contract.skill_id == f"aa-{family}-plan"
        elif stage == "plan-review":
            assert contract.input_model is PlanInputV1
            assert contract.agent_result_model is PlanReviewAuthoring
            assert contract.output_model is PlanReview
            assert contract.agent_profile == _PLAN_REVIEW_PROFILE
            assert contract.skill_id == f"aa-{family}-plan-reviewer"
        elif stage == "codegen":
            assert contract.input_model is CodegenInputV1
            assert contract.agent_result_model is CodegenAuthoringV1
            assert contract.output_model is CodegenResultV1
            assert contract.agent_profile == _CODEGEN_PROFILE
            assert contract.skill_id == f"aa-{family}-codegen"
        else:
            pytest.fail(f"unexpected generation stage: {stage}")


def test_generation_plugin_projects_authenticated_attempt_contracts() -> None:
    refs = attempt_contract_refs()
    contribution = GenerationPlugin.contribute(RegistryPorts(engine_api=ENGINE_API_VERSION))
    assert refs == contribution.attempt_contracts == GenerationPlugin.descriptor().attempt_contracts
    assert all(isinstance(item, AttemptContractRef) for item in refs)
    assert len(contribution.commit_validators) == 7
    assert all(contract.validators == () for contract in AGENT_JOB_CONTRACTS.values())


def test_generation_pure_ids_are_not_task_contracts() -> None:
    published = {contract.contract_id for contract in AGENT_JOB_CONTRACTS.values()}
    assert "assurance.generation.complete" not in published
    assert "assurance.generation.review-round.advance" not in published
    assert "context" not in inspect.signature(complete_generation).parameters
    assert "context" not in inspect.signature(advance_review_round).parameters


async def test_complete_generation_matches_legacy_handler_without_touching_context() -> None:
    payload = {"completed": [{"value": True}] * 4, "selected_families": ["api", "fuzz"]}
    spy = MagicMock(spec=TaskContext)
    executed = await execute_task(
        GenerationCompleteHandler(),
        payload,
        capability_id="assurance.generation.complete",
    )
    output = complete_generation(payload)
    assert isinstance(output, GenerationCompletionOutput)
    assert output.model_dump(mode="json") == executed.outcome.output
    assert spy.mock_calls == []


@pytest.mark.parametrize(
    "payload",
    [
        {"completed": [{"value": True}] * 3, "selected_families": ["api"]},
        {"completed": [{"value": True}] * 4, "selected_families": []},
        {"completed": [{"value": True}] * 4, "selected_families": ["api", "api"]},
        {"completed": [{"value": False}] * 4, "selected_families": ["api"]},
    ],
)
def test_complete_generation_rejects_invalid_inputs(payload: dict[str, object]) -> None:
    with pytest.raises((ValidationError, ValueError)):
        complete_generation(payload)


@pytest.mark.parametrize("family", _FAMILIES)
@pytest.mark.parametrize("stage", ("plan",))
async def test_advance_generation_review_round_matches_legacy_handler(family: str, stage: str) -> None:
    payload = {"family": family, "stage": stage, "rounds_used": 0, "rounds_budget": 2}
    spy = MagicMock(spec=TaskContext)
    executed = await execute_task(
        GenerationReviewRoundAdvanceHandler(),
        payload,
        capability_id="assurance.generation.review-round.advance",
    )
    output = advance_review_round(payload)
    assert isinstance(output, GenerationReviewRoundAdvanceOutput)
    assert output.model_dump(mode="json") == executed.outcome.output
    assert spy.mock_calls == []


@pytest.mark.parametrize(
    "payload",
    [
        {"family": "api", "stage": "plan", "rounds_used": 2, "rounds_budget": 2},
        {"family": "unknown", "stage": "plan", "rounds_used": 0, "rounds_budget": 2},
        {"family": "api", "stage": "review", "rounds_used": 0, "rounds_budget": 2},
        {"family": "api", "stage": "plan", "rounds_used": 0, "rounds_budget": 2, "extra": True},
    ],
)
def test_advance_generation_review_round_rejects_invalid_inputs(payload: dict[str, object]) -> None:
    with pytest.raises((ValidationError, ValueError)):
        advance_review_round(payload)
