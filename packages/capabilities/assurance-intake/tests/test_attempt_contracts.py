from __future__ import annotations

import pytest

from graph_engine import ENGINE_API_VERSION, RegistryPorts
from graph_engine.attempts import TaskAttemptContract
from graph_engine.plugin_api import AttemptContractRef

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
from agent_runtime_contracts.ops import ArtifactListResultV1
from assurance_intake.contracts.agent import FinalizedArtifactsV1
from assurance_intake.ops.case_design import CaseDesignOutputV1, CaseDesignInputV1
from assurance_intake.ops.case_repair import CaseRepairInputV1, CaseRepairOutputV1
from assurance_intake.ops.case_review import CaseReviewInputV1
from assurance_intake.ops.explore import ExploreInputV1
from assurance_intake.ops.intake import IntakeInputV1
from assurance_intake.feature import AGENT_JOB_CONTRACTS, TASK_ATTEMPT_CONTRACTS, attempt_contract_refs
from assurance_intake.contracts.review import CaseReviewOutputV1, CaseReviewResultV1
from assurance_intake.plugin import IntakePlugin
from assurance_quality.contracts.attempts import AGENT_JOB_CONTRACTS as QUALITY_AGENT_JOBS
from assurance_quality.plugin import QualityPlugin

EXPECTED_AGENT_COUNTS = {
    "assurance.intake": 5,
    "assurance.generation": 8,
    "assurance.execution": 0,
    "assurance.quality": 5,
    "assurance.healing": 2,
    "assurance.improvement": 6,
}

EXPECTED_VALIDATOR_COUNTS = {
    "assurance.intake": 1,
    "assurance.generation": 0,
    "assurance.execution": 0,
    "assurance.quality": 0,
    "assurance.healing": 0,
    "assurance.improvement": 0,
}

_PURE_IDS = frozenset(
    {
        "assurance.generation.complete",
        "assurance.generation.review-round.advance",
        "assurance.healing.repair-round.advance",
    }
)


def test_case_review_seal_files_are_finalize_not_runtime() -> None:
    claims = AGENT_JOB_CONTRACTS["case-review"].phase_write_claims
    assert claims.finalize == (
        "qa/cases/reviewed-case.json",
        "qa/cases/reviews",
        "qa/results/cases/epochs",
    )
    assert claims.runtime == (
        "qa/results/review/case-review-summary.md",
        "qa/results/review/case-review.json",
    )


def test_review_history_identity_includes_epoch() -> None:
    from assurance_intake.ops.case_review.hooks.seal import case_review_runtime_paths

    _, first, _ = case_review_runtime_paths(coverage_epoch=0, review_round=0)
    _, second, _ = case_review_runtime_paths(coverage_epoch=1, review_round=0)
    assert first == "qa/cases/reviews/epochs/0/rounds/0.json"
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
    assert sum(EXPECTED_AGENT_COUNTS.values()) == 26


def test_intake_agent_catalog_uses_concrete_models_and_seal_validator() -> None:
    expected = {
        "case-design": (
            "assurance.intake.agent.case-design.v1",
            "aa-case-design",
            "assurance-v1-doc-author",
            CaseDesignInputV1,
            ArtifactListResultV1,
            CaseDesignOutputV1,
        ),
        "case-repair": (
            "assurance.intake.agent.case-repair.v1",
            "aa-case-repair",
            "assurance-v1-doc-author",
            CaseRepairInputV1,
            ArtifactListResultV1,
            CaseRepairOutputV1,
        ),
        "case-review": (
            "assurance.intake.agent.case-review.v1",
            "aa-case-reviewer",
            "assurance-v1-reviewer",
            CaseReviewInputV1,
            CaseReviewResultV1,
            CaseReviewOutputV1,
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
        assert contract.validators == ("assurance.intake.validator.sealed-artifact-refs.v1",)
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


def test_registered_validators_are_bound_only_where_required() -> None:
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
            assert contract.validators == (
                ("assurance.intake.validator.sealed-artifact-refs.v1",) if owner == "assurance.intake" else ()
            )
            effectful += 1
    for contract in IMPROVEMENT_TASKS.values():
        assert isinstance(contract, TaskAttemptContract)
        assert contract.validators == ()
        effectful += 1
    assert registered == 1
    assert effectful == 37
    assert set(IMPROVEMENT_TASKS).isdisjoint(_PURE_IDS)


def test_semantic_agent_contracts_are_twenty_six() -> None:
    catalogs = (
        AGENT_JOB_CONTRACTS,
        GENERATION_AGENT_JOBS,
        EXECUTION_AGENT_JOBS,
        QUALITY_AGENT_JOBS,
        HEALING_AGENT_JOBS,
        IMPROVEMENT_AGENT_JOBS,
    )
    assert sum(len(catalog) for catalog in catalogs) == 26


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


def test_intake_owns_resolve_plan_task() -> None:
    assert tuple(TASK_ATTEMPT_CONTRACTS) == ("coverage-rework", "resolve-plan")
    resolve = TASK_ATTEMPT_CONTRACTS["resolve-plan"]
    assert resolve.contract_id == "assurance.intake.task.resolve-plan"
    assert resolve.handler_id == "assurance.intake.resolve-plan"
    assert resolve.resources.writes == (
        "qa/results/explore/exploration.json",
        "qa/results/plan",
        "qa/results/preparation/refs.json",
    )


def test_review_round_advance_is_not_a_task_contract() -> None:
    assert all(
        contract.contract_id != "assurance.intake.review-round.advance"
        for contract in AGENT_JOB_CONTRACTS.values()
    )
