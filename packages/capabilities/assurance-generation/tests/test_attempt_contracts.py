from __future__ import annotations


import pytest
from pydantic import ValidationError

from graph_engine import ENGINE_API_VERSION, RegistryPorts
from graph_engine.plugin_api import AttemptContractRef

from agent_runtime_contracts import AgentExecutionContract
from assurance_generation.contracts.agent import CodegenBoundInputV1
from assurance_generation.contracts.attempts import (
    AGENT_JOB_CONTRACTS,
    TASK_ATTEMPT_CONTRACTS,
    attempt_contract_refs,
)
from assurance_generation.contracts.codegen import (
    CodegenAuthoringV1,
    CodegenResultV1,
)
from assurance_generation.contracts.decisions import complete_generation
from assurance_generation.contracts.reviews import PlanReview, PlanReviewAuthoring
from assurance_generation.plugin import GenerationPlugin

_FAMILIES = ("api", "e2e", "fuzz", "performance")
_PLAN_REVIEW_PROFILE = "assurance-v1-reviewer"
_PLAN_PROFILE = "assurance-v1-doc-author"
_CODEGEN_PROFILE = "assurance-v1-test-author"


def test_generation_round_history_sits_under_the_review_claim() -> None:
    from assurance_generation.contracts.attempts import AGENT_JOB_CONTRACTS, OUTPUT_ROUTE_TEMPLATES

    claim = "qa/results/codegen/api/reviews"
    contract = AGENT_JOB_CONTRACTS["api.codegen-review"]
    assert claim in contract.phase_write_claims.finalize
    assert claim in contract.resources.writes
    assert all("{" not in path for path in OUTPUT_ROUTE_TEMPLATES["api.codegen-review"])
    history = f"{claim}/epochs/{{coverage_epoch}}/rounds/{{review_round}}.json"
    assert history.format(coverage_epoch=0, review_round=0) != history.format(
        coverage_epoch=1, review_round=0
    )


def test_generation_owns_twelve_agent_contracts_and_two_tasks() -> None:
    assert len(AGENT_JOB_CONTRACTS) == 8
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
        "api.codegen-review",
        "e2e.codegen",
        "e2e.codegen-review",
        "fuzz.codegen",
        "fuzz.codegen-review",
        "performance.codegen",
        "performance.codegen-review",
    )
    assert tuple(sorted(AGENT_JOB_CONTRACTS)) == tuple(sorted(expected_bases))
    for base, contract in AGENT_JOB_CONTRACTS.items():
        family, _, stage = base.partition(".")
        assert contract.contract_id == f"assurance.generation.agent.{base}.v1"
        assert contract.prepare_handler_id == f"assurance.generation.{base}.prepare"
        assert contract.finalize_handler_id == f"assurance.generation.{base}.finalize"
        if stage == "codegen-review":
            assert contract.input_model is CodegenBoundInputV1
            assert contract.agent_result_model is PlanReviewAuthoring
            assert contract.output_model is PlanReview
            assert contract.agent_profile == _PLAN_REVIEW_PROFILE
            assert contract.skill_id == f"aa-{family}-codegen-reviewer"
        elif stage == "codegen":
            assert contract.input_model is CodegenBoundInputV1
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
    assert contribution.commit_validators == {}
    assert all(contract.validators == () for contract in AGENT_JOB_CONTRACTS.values())


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
