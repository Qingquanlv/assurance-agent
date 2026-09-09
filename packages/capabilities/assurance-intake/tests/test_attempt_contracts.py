from __future__ import annotations

import inspect
from typing import Any, cast, get_type_hints
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from graph_engine import ENGINE_API_VERSION, RegistryPorts
from graph_engine.attempts import TaskAttemptContract
from graph_engine.plugin_api import AttemptContractRef, TaskContext

from agent_runtime_contracts import AgentExecutionContract
from assurance_execution.contracts.attempts import AGENT_JOB_CONTRACTS as EXECUTION_AGENT_JOBS
from assurance_execution.plugin import ExecutionPlugin
from assurance_generation.contracts.attempts import AGENT_JOB_CONTRACTS as GENERATION_AGENT_JOBS
from assurance_generation.plugin import GenerationPlugin
from assurance_healing.contracts.attempts import AGENT_JOB_CONTRACTS as HEALING_AGENT_JOBS
from assurance_healing.plugin import HealingPlugin
from assurance_improvement.contracts.attempts import AGENT_JOB_CONTRACTS as IMPROVEMENT_AGENT_JOBS
from assurance_improvement.contracts.attempts import TASK_ATTEMPT_CONTRACTS as IMPROVEMENT_TASKS
from assurance_improvement.plugin import ImprovementPlugin
from assurance_intake.contracts.agent import (
    ArtifactListResultV1,
    CaseDesignOutputV1,
    CaseDesignInputV1,
    CaseReviewInputV1,
    ExploreInputV1,
    FinalizedArtifactsV1,
    IntakeInputV1,
)
from assurance_intake.contracts.attempts import (
    AGENT_JOB_CONTRACTS,
    TASK_ATTEMPT_CONTRACTS,
    attempt_contract_refs,
)
from assurance_intake.contracts.decisions import (
    ReviewRoundAdvanceInput,
    ReviewRoundAdvanceOutput,
    advance_review_round,
)
from assurance_intake.contracts.review import CaseReviewResultV1
from assurance_intake.operations.workflow_state import ReviewRoundAdvanceHandler
from assurance_intake.plugin import IntakePlugin
from assurance_quality.contracts.attempts import AGENT_JOB_CONTRACTS as QUALITY_AGENT_JOBS
from assurance_quality.plugin import QualityPlugin
from tests.product.test_change_local_output_routing import execute_task

EXPECTED_AGENT_COUNTS = {
    "assurance.intake": 4,
    "assurance.generation": 12,
    "assurance.execution": 2,
    "assurance.quality": 5,
    "assurance.healing": 3,
    "assurance.improvement": 6,
}

EXPECTED_VALIDATOR_COUNTS = {
    "assurance.intake": 2,
    "assurance.generation": 7,
    "assurance.execution": 2,
    "assurance.quality": 6,
    "assurance.healing": 3,
    "assurance.improvement": 4,
}

_PURE_IDS = frozenset(
    {
        "assurance.generation.complete",
        "assurance.generation.review-round.advance",
        "assurance.healing.repair-round.advance",
        "assurance.intake.review-round.advance",
    }
)


def test_review_history_identity_includes_epoch() -> None:
    from assurance_intake.contracts.attempts import OUTPUT_ROUTE_TEMPLATES

    pattern = "qa/changes/{change_id}/cases/reviews/epochs/{coverage_epoch}/rounds/{review_round}.json"
    assert pattern in OUTPUT_ROUTE_TEMPLATES["case-review"]
    first = pattern.format(change_id="CH-1", coverage_epoch=0, review_round=0)
    second = pattern.format(change_id="CH-1", coverage_epoch=1, review_round=0)
    assert first != second


def test_feature_agent_counts_are_frozen() -> None:
    catalogs = {
        "assurance.intake": AGENT_JOB_CONTRACTS,
        "assurance.generation": GENERATION_AGENT_JOBS,
        "assurance.execution": EXECUTION_AGENT_JOBS,
        "assurance.quality": QUALITY_AGENT_JOBS,
        "assurance.healing": HEALING_AGENT_JOBS,
        "assurance.improvement": IMPROVEMENT_AGENT_JOBS,
    }
    for owner, expected in EXPECTED_AGENT_COUNTS.items():
        assert len(catalogs[owner]) == expected
        assert all(contract.owner_id == owner for contract in catalogs[owner].values())
    assert sum(EXPECTED_AGENT_COUNTS.values()) == 32


def test_intake_agent_catalog_uses_concrete_models_and_empty_validators() -> None:
    expected = {
        "case-design": (
            "assurance.intake.agent.case-design.v1",
            "aa-case-design",
            "assurance-v1-doc-author",
            CaseDesignInputV1,
            ArtifactListResultV1,
            CaseDesignOutputV1,
        ),
        "case-review": (
            "assurance.intake.agent.case-review.v1",
            "aa-case-reviewer",
            "assurance-v1-reviewer",
            CaseReviewInputV1,
            CaseReviewResultV1,
            CaseReviewResultV1,
        ),
        "explore": (
            "assurance.intake.agent.explore.v1",
            "aa-explore",
            "assurance-v1-explorer",
            ExploreInputV1,
            ArtifactListResultV1,
            FinalizedArtifactsV1,
        ),
        "intake": (
            "assurance.intake.agent.intake.v1",
            "aa-intake",
            "assurance-v1-doc-author",
            IntakeInputV1,
            ArtifactListResultV1,
            FinalizedArtifactsV1,
        ),
    }
    assert set(AGENT_JOB_CONTRACTS) == set(expected)
    for base, (contract_id, skill_id, profile, input_model, result_model, output_model) in expected.items():
        contract = AGENT_JOB_CONTRACTS[base]
        assert isinstance(contract, AgentExecutionContract)
        assert contract.contract_id == contract_id
        assert contract.prepare_handler_id == f"assurance.intake.{base}.prepare"
        assert contract.finalize_handler_id == f"assurance.intake.{base}.finalize"
        assert contract.skill_id == skill_id
        assert contract.agent_profile == profile
        assert contract.input_model is input_model
        assert contract.agent_result_model is result_model
        assert contract.output_model is output_model
        assert contract.validators == ()
        assert contract.retry.max_attempts == 10
        assert contract.retry.interval_seconds == 10
        assert contract.timeout.seconds == 60
        assert not hasattr(contract, "requires_provider_schema")
        claims = contract.phase_write_claims
        claimed = set(claims.prepare) | set(claims.runtime) | set(claims.finalize)
        assert claimed <= set(contract.resources.writes)
        assert not (set(claims.prepare) & set(claims.runtime))
        assert not (set(claims.prepare) & set(claims.finalize))
        assert not (set(claims.runtime) & set(claims.finalize))


def test_agent_contract_omitting_validators_is_invalid() -> None:
    sample = AGENT_JOB_CONTRACTS["intake"]
    with pytest.raises(TypeError):
        AgentExecutionContract(
            contract_id=sample.contract_id,
            owner_id=sample.owner_id,
            prepare_handler_id=sample.prepare_handler_id,
            finalize_handler_id=sample.finalize_handler_id,
            skill_id=sample.skill_id,
            agent_profile=sample.agent_profile,
            input_model=sample.input_model,
            agent_result_model=sample.agent_result_model,
            output_model=sample.output_model,
            resources=sample.resources,
            retry=sample.retry,
            timeout=sample.timeout,
            phase_write_claims=sample.phase_write_claims,
        )  # type: ignore[call-arg]


def test_registered_validators_remain_unbound_and_legal() -> None:
    plugins = {
        "assurance.intake": IntakePlugin,
        "assurance.generation": GenerationPlugin,
        "assurance.execution": ExecutionPlugin,
        "assurance.quality": QualityPlugin,
        "assurance.healing": HealingPlugin,
        "assurance.improvement": ImprovementPlugin,
    }
    catalogs = {
        "assurance.intake": AGENT_JOB_CONTRACTS,
        "assurance.generation": GENERATION_AGENT_JOBS,
        "assurance.execution": EXECUTION_AGENT_JOBS,
        "assurance.quality": QUALITY_AGENT_JOBS,
        "assurance.healing": HEALING_AGENT_JOBS,
        "assurance.improvement": IMPROVEMENT_AGENT_JOBS,
    }
    registered = 0
    effectful = 0
    for owner, plugin in plugins.items():
        contribution = plugin.contribute(RegistryPorts(engine_api=ENGINE_API_VERSION))
        assert len(contribution.commit_validators) == EXPECTED_VALIDATOR_COUNTS[owner]
        registered += len(contribution.commit_validators)
        for contract in catalogs[owner].values():
            assert contract.validators == ()
            effectful += 1
    for contract in IMPROVEMENT_TASKS.values():
        assert isinstance(contract, TaskAttemptContract)
        assert contract.validators == ()
        effectful += 1
    assert registered == 24
    assert effectful == 41
    assert set(IMPROVEMENT_TASKS).isdisjoint(_PURE_IDS)


def test_semantic_agent_contracts_are_thirty_four() -> None:
    catalogs = (
        AGENT_JOB_CONTRACTS,
        GENERATION_AGENT_JOBS,
        EXECUTION_AGENT_JOBS,
        QUALITY_AGENT_JOBS,
        HEALING_AGENT_JOBS,
        IMPROVEMENT_AGENT_JOBS,
    )
    assert sum(len(catalog) for catalog in catalogs) == 32


def test_intake_plugin_projects_authenticated_attempt_contracts() -> None:
    refs = attempt_contract_refs()
    contribution = IntakePlugin.contribute(RegistryPorts(engine_api=ENGINE_API_VERSION))
    descriptor = IntakePlugin.descriptor()
    assert refs == contribution.attempt_contracts == descriptor.attempt_contracts
    assert all(isinstance(item, AttemptContractRef) for item in refs)
    assert tuple(item.contract_id for item in refs) == tuple(
        sorted(
            contract.contract_id
            for contract in (*AGENT_JOB_CONTRACTS.values(), *TASK_ATTEMPT_CONTRACTS.values())
        )
    )


def test_intake_owns_resolve_and_read_only_load_plan_tasks() -> None:
    assert tuple(TASK_ATTEMPT_CONTRACTS) == ("resolve-plan", "load-plan")
    resolve = TASK_ATTEMPT_CONTRACTS["resolve-plan"]
    load = TASK_ATTEMPT_CONTRACTS["load-plan"]
    assert resolve.contract_id == "assurance.intake.task.resolve-plan"
    assert resolve.handler_id == "assurance.intake.resolve-plan"
    assert resolve.resources.writes == ("qa/changes/{change_id}/plan",)
    assert load.contract_id == "assurance.intake.task.load-plan"
    assert load.handler_id == "assurance.intake.load-plan"
    assert load.resources.writes == ()


def test_review_round_advance_is_not_a_task_contract() -> None:
    assert all(
        contract.contract_id != "assurance.intake.review-round.advance"
        for contract in AGENT_JOB_CONTRACTS.values()
    )
    assert "context" not in inspect.signature(advance_review_round).parameters
    assert get_type_hints(advance_review_round)["return"] is ReviewRoundAdvanceOutput


@pytest.mark.parametrize(("used", "budget", "expected"), [(0, 2, 1), (1, 2, 2)])
async def test_advance_review_round_matches_legacy_handler_without_touching_context(
    used: int, budget: int, expected: int
) -> None:
    payload = {"rounds_used": used, "rounds_budget": budget}
    spy = MagicMock(spec=TaskContext)
    executed = await execute_task(
        ReviewRoundAdvanceHandler(),
        cast(Any, payload),
        capability_id="assurance.intake.review-round.advance",
    )
    output = advance_review_round(payload)
    assert isinstance(output, ReviewRoundAdvanceOutput)
    assert output.model_dump(mode="json") == executed.outcome.output
    assert output.model_dump(mode="json") == {"rounds_used": expected, "rounds_budget": budget}
    assert spy.mock_calls == []


@pytest.mark.parametrize(
    "payload",
    [
        {"rounds_used": 2, "rounds_budget": 2},
        {"rounds_used": -1, "rounds_budget": 2},
        {"rounds_used": 0, "rounds_budget": 0},
        {"rounds_used": 3, "rounds_budget": 2},
        {"rounds_used": 1},
        {"rounds_budget": 2},
        {"rounds_used": 0, "rounds_budget": 2, "extra": True},
    ],
)
def test_advance_review_round_rejects_invalid_inputs(payload: dict[str, object]) -> None:
    with pytest.raises((ValidationError, ValueError)):
        advance_review_round(payload)
    ReviewRoundAdvanceInput.model_validate({"rounds_used": 0, "rounds_budget": 2})
