from __future__ import annotations

import pytest

from graph_engine import ENGINE_API_VERSION, RegistryPorts
from graph_engine.attempts import TaskAttemptContract
from graph_engine.plugin_api import AttemptContractRef

from agent_runtime_contracts import AgentExecutionContract
from assurance_improvement.contracts.agent import (
    ArchiveResultV1,
    ImprovementReviewResultV1,
    ImprovementSkillInputV1,
    RetroAnalysisResultV3,
    RetroAnalysisInputV1,
    RetroSynthesisInputV1,
)
from assurance_improvement.contracts.attempts import (
    AGENT_JOB_CONTRACTS,
    TASK_ATTEMPT_CONTRACTS,
    attempt_contract_refs,
)
from assurance_improvement.contracts.delivery import (
    ChangeExportReceipt,
    MemoryApplyReceipt,
    MemoryEvalReceipt,
    MemoryRollbackReceipt,
)
from assurance_improvement.contracts.improvements import ImprovementProjection
from assurance_improvement.contracts.retro import RetroBuildSlicesInputV1
from assurance_improvement.contracts.review import AppliedAutoReviewV1
from assurance_improvement.operations.delivery import (
    ApplyMemoryInput,
    EvaluateMemoryInput,
    ExportChangeInput,
    RollbackMemoryInput,
)
from assurance_improvement.contracts.retro import (
    RetroReconcileInputV1,
    RetroReconcileResultV1,
    RetroCollectedV1,
    RetroCollectInput,
)
from assurance_improvement.operations.review import ApplyAutoReviewInput, ApplyReviewInput
from assurance_improvement.plugin import ImprovementPlugin

_EFFECTFUL_TASK_IDS = (
    "assurance.improvement.retro-build-slices",
    "assurance.improvement.retro-collect-v3",
    "assurance.improvement.reconcile-improvements",
    "assurance.improvement.evaluate-memory-improvement",
    "assurance.improvement.export-change-improvement",
    "assurance.improvement.apply-improvement-auto-review",
    "assurance.improvement.apply-improvement-review",
    "assurance.improvement.apply-memory-improvement",
    "assurance.improvement.rollback-memory-improvement",
)


def test_improvement_owns_six_agent_contracts() -> None:
    expected = {
        "archive": ("aa-archive", "assurance-v1-archiver", ArchiveResultV1),
        "improvement-review": (
            "aa-improvement-reviewer",
            "assurance-v1-reviewer",
            ImprovementReviewResultV1,
        ),
        "retro-eval-analysis": ("aa-retro-eval-analysis", "assurance-v1-doc-author", RetroAnalysisResultV3),
        "retro-issue-analysis": (
            "aa-retro-issue-analysis",
            "assurance-v1-doc-author",
            RetroAnalysisResultV3,
        ),
        "retro-workflow-analysis": (
            "aa-retro-workflow-analysis",
            "assurance-v1-doc-author",
            RetroAnalysisResultV3,
        ),
        "retro": ("aa-retro", "assurance-v1-doc-author", RetroAnalysisResultV3),
    }
    assert len(AGENT_JOB_CONTRACTS) == 6
    assert set(AGENT_JOB_CONTRACTS) == set(expected)
    for base, (skill_id, profile, result_model) in expected.items():
        contract = AGENT_JOB_CONTRACTS[base]
        assert isinstance(contract, AgentExecutionContract)
        assert contract.contract_id == f"assurance.improvement.agent.{base}.v1"
        assert contract.owner_id == "assurance.improvement"
        assert contract.prepare_handler_id == f"assurance.improvement.{base}.prepare"
        assert contract.finalize_handler_id == f"assurance.improvement.{base}.finalize"
        assert contract.skill_id == skill_id
        assert contract.agent_profile == profile
        assert contract.input_model is (
            RetroSynthesisInputV1
            if base == "retro"
            else RetroAnalysisInputV1
            if base.startswith("retro-")
            else ImprovementSkillInputV1
        )
        assert contract.agent_result_model is result_model
        assert contract.output_model is result_model
        assert contract.validators == ()
        assert contract.retry.max_attempts == 10
        assert contract.retry.interval_seconds == 10
        assert contract.timeout.seconds == 60
        claims = contract.phase_write_claims
        assert set(claims.runtime) == set(contract.resources.writes)


def test_nine_effectful_improvement_ids_are_task_contracts() -> None:
    expected_models = {
        "assurance.improvement.retro-build-slices": (RetroBuildSlicesInputV1, RetroCollectInput),
        "assurance.improvement.retro-collect-v3": (RetroCollectInput, RetroCollectedV1),
        "assurance.improvement.reconcile-improvements": (RetroReconcileInputV1, RetroReconcileResultV1),
        "assurance.improvement.evaluate-memory-improvement": (EvaluateMemoryInput, MemoryEvalReceipt),
        "assurance.improvement.export-change-improvement": (ExportChangeInput, ChangeExportReceipt),
        "assurance.improvement.apply-improvement-auto-review": (
            ApplyAutoReviewInput,
            AppliedAutoReviewV1,
        ),
        "assurance.improvement.apply-improvement-review": (ApplyReviewInput, ImprovementProjection),
        "assurance.improvement.apply-memory-improvement": (ApplyMemoryInput, MemoryApplyReceipt),
        "assurance.improvement.rollback-memory-improvement": (RollbackMemoryInput, MemoryRollbackReceipt),
    }
    assert tuple(sorted(TASK_ATTEMPT_CONTRACTS)) == tuple(sorted(_EFFECTFUL_TASK_IDS))
    for handler_id, (input_model, output_model) in expected_models.items():
        contract = TASK_ATTEMPT_CONTRACTS[handler_id]
        assert isinstance(contract, TaskAttemptContract)
        suffix = handler_id.removeprefix("assurance.improvement.")
        expected_id = (
            "assurance.improvement.retro-build-slices"
            if handler_id == "assurance.improvement.retro-build-slices"
            else f"assurance.improvement.task.{suffix}"
        )
        assert contract.contract_id == expected_id
        assert contract.owner_id == "assurance.improvement"
        expected_handler = (
            "assurance.improvement.retro-build-slices.execute"
            if handler_id == "assurance.improvement.retro-build-slices"
            else handler_id
        )
        assert contract.handler_id == expected_handler
        assert contract.input_model is input_model
        assert contract.output_model is output_model
        assert contract.validators == ()
        assert contract.retry.max_attempts == 1
        assert contract.timeout.seconds == 60


def test_evaluate_memory_improvement_is_effectful_not_a_pure_function() -> None:
    contract = TASK_ATTEMPT_CONTRACTS["assurance.improvement.evaluate-memory-improvement"]
    assert isinstance(contract, TaskAttemptContract)
    assert contract.output_model is MemoryEvalReceipt
    assert contract.validators == ()
    with pytest.raises(TypeError):
        TaskAttemptContract(
            contract_id=contract.contract_id,
            owner_id=contract.owner_id,
            handler_id=contract.handler_id,
            input_model=contract.input_model,
            output_model=contract.output_model,
            resources=contract.resources,
            retry=contract.retry,
            timeout=contract.timeout,
        )  # type: ignore[call-arg]


def test_improvement_plugin_projects_owner_contracts() -> None:
    refs = attempt_contract_refs()
    contribution = ImprovementPlugin.contribute(RegistryPorts(engine_api=ENGINE_API_VERSION))
    assert refs == contribution.attempt_contracts == ImprovementPlugin.descriptor().attempt_contracts
    assert all(isinstance(item, AttemptContractRef) for item in refs)
    assert len(refs) == 15
    assert {item.contract_id for item in refs} == {
        *(contract.contract_id for contract in AGENT_JOB_CONTRACTS.values()),
        *(contract.contract_id for contract in TASK_ATTEMPT_CONTRACTS.values()),
    }
    assert len(contribution.commit_validators) == 4
    assert all(contract.validators == () for contract in AGENT_JOB_CONTRACTS.values())
    assert all(contract.validators == () for contract in TASK_ATTEMPT_CONTRACTS.values())
