from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, cast

from agent_runtime_contracts import AgentExecutionContract
from graph_engine.attempts import AttemptRetryPolicy, AttemptTimeoutPolicy, TaskAttemptContract
from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.plugin_api import AttemptContractRef, ResourceClaimTemplate, ResourceClaims

from assurance_improvement.contracts.agent import (
    ArchiveResultV1,
    ImprovementReviewResultV1,
    ImprovementSkillInputV1,
    RetroAnalysisResultV3,
)
from assurance_improvement.contracts.delivery import (
    ChangeExportReceipt,
    MemoryApplyReceipt,
    MemoryEvalReceipt,
    MemoryRollbackReceipt,
)
from assurance_improvement.contracts.improvements import ImprovementLedgerProjection, ImprovementProjection
from assurance_improvement.contracts.review import ImprovementAutoReviewStatus
from assurance_improvement.operations.delivery import (
    ApplyMemoryInput,
    EvaluateMemoryInput,
    ExportChangeInput,
    RollbackMemoryInput,
)
from assurance_improvement.operations.retro import ReconcileInput, RetroCollectInput
from assurance_improvement.operations.review import ApplyAutoReviewInput, ApplyReviewInput

_ARCHIVER = "assurance-v1-archiver"
_DOC_AUTHOR = "assurance-v1-doc-author"
_REVIEWER = "assurance-v1-reviewer"
_RETRY = AttemptRetryPolicy(max_attempts=1)
_TIMEOUT = AttemptTimeoutPolicy(seconds=60)


def _paths(*suffixes: str) -> tuple[str, ...]:
    return tuple(sorted(f"qa/changes/{{change_id}}/{suffix}" for suffix in suffixes))


def _job(
    base: str,
    skill_id: str,
    agent_profile: str,
    result_model: type[Any],
    outputs: tuple[str, ...],
) -> AgentExecutionContract[Any, Any, Any]:
    return AgentExecutionContract(
        contract_id=f"assurance.improvement.agent.{base}.v1",
        owner_id="assurance.improvement",
        prepare_handler_id=f"assurance.improvement.{base}.prepare",
        finalize_handler_id=f"assurance.improvement.{base}.finalize",
        skill_id=skill_id,
        agent_profile=agent_profile,
        input_model=ImprovementSkillInputV1,
        agent_result_model=result_model,
        output_model=result_model,
        requires_provider_schema=True,
        resources=ResourceClaimTemplate(
            parameters={"change_id": "/workspace/scope_id"},
            reads=("qa",),
            writes=_paths(*outputs),
        ),
        retry=_RETRY,
        timeout=_TIMEOUT,
        validators=(),
    )


def _task(
    handler_id: str,
    input_model: type[Any],
    output_model: type[Any],
) -> TaskAttemptContract[Any, Any]:
    suffix = handler_id.removeprefix("assurance.improvement.")
    return TaskAttemptContract(
        contract_id=f"assurance.improvement.task.{suffix}",
        owner_id="assurance.improvement",
        handler_id=handler_id,
        input_model=input_model,
        output_model=output_model,
        resources=ResourceClaims(),
        retry=_RETRY,
        timeout=_TIMEOUT,
        validators=(),
    )


_JOBS: tuple[tuple[str, str, str, type[Any], tuple[str, ...]], ...] = (
    ("archive", "aa-archive", _ARCHIVER, ArchiveResultV1, ("archive/archive-receipt.json",)),
    (
        "improvement-review",
        "aa-improvement-reviewer",
        _REVIEWER,
        ImprovementReviewResultV1,
        ("review/improvement-review.json",),
    ),
    (
        "retro-eval-analysis",
        "aa-retro-eval-analysis",
        _DOC_AUTHOR,
        RetroAnalysisResultV3,
        ("retro/retro-eval-analysis.json",),
    ),
    (
        "retro-issue-analysis",
        "aa-retro-issue-analysis",
        _DOC_AUTHOR,
        RetroAnalysisResultV3,
        ("retro/retro-issue-analysis.json",),
    ),
    (
        "retro-workflow-analysis",
        "aa-retro-workflow-analysis",
        _DOC_AUTHOR,
        RetroAnalysisResultV3,
        ("retro/retro-workflow-analysis.json",),
    ),
    ("retro", "aa-retro", _DOC_AUTHOR, RetroAnalysisResultV3, ("retro/retro.json",)),
)

AGENT_JOB_CONTRACTS: Mapping[str, AgentExecutionContract[Any, Any, Any]] = MappingProxyType(
    {
        base: _job(base, skill_id, profile, result_model, outputs)
        for base, skill_id, profile, result_model, outputs in _JOBS
    }
)
OUTPUT_ROUTE_TEMPLATES: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {base: _paths(*outputs) for base, _skill, _profile, _result, outputs in _JOBS}
)
TASK_ATTEMPT_CONTRACTS: Mapping[str, TaskAttemptContract[Any, Any]] = MappingProxyType(
    {
        "assurance.improvement.apply-improvement-auto-review": _task(
            "assurance.improvement.apply-improvement-auto-review",
            ApplyAutoReviewInput,
            ImprovementAutoReviewStatus,
        ),
        "assurance.improvement.apply-improvement-review": _task(
            "assurance.improvement.apply-improvement-review",
            ApplyReviewInput,
            ImprovementProjection,
        ),
        "assurance.improvement.apply-memory-improvement": _task(
            "assurance.improvement.apply-memory-improvement",
            ApplyMemoryInput,
            MemoryApplyReceipt,
        ),
        "assurance.improvement.evaluate-memory-improvement": _task(
            "assurance.improvement.evaluate-memory-improvement",
            EvaluateMemoryInput,
            MemoryEvalReceipt,
        ),
        "assurance.improvement.export-change-improvement": _task(
            "assurance.improvement.export-change-improvement",
            ExportChangeInput,
            ChangeExportReceipt,
        ),
        "assurance.improvement.reconcile-improvements": _task(
            "assurance.improvement.reconcile-improvements",
            ReconcileInput,
            ImprovementLedgerProjection,
        ),
        "assurance.improvement.retro-collect-v3": _task(
            "assurance.improvement.retro-collect-v3",
            RetroCollectInput,
            RetroCollectInput,
        ),
        "assurance.improvement.rollback-memory-improvement": _task(
            "assurance.improvement.rollback-memory-improvement",
            RollbackMemoryInput,
            MemoryRollbackReceipt,
        ),
    }
)


def attempt_contract_refs() -> tuple[AttemptContractRef, ...]:
    contracts: tuple[AgentExecutionContract[Any, Any, Any] | TaskAttemptContract[Any, Any], ...] = (
        *AGENT_JOB_CONTRACTS.values(),
        *TASK_ATTEMPT_CONTRACTS.values(),
    )
    return tuple(
        sorted(
            (
                AttemptContractRef(
                    contract_id=contract.contract_id,
                    digest=canonical_digest(cast(JSONValue, contract.canonical_projection())),
                )
                for contract in contracts
            ),
            key=lambda item: item.contract_id,
        )
    )


__all__ = [
    "AGENT_JOB_CONTRACTS",
    "OUTPUT_ROUTE_TEMPLATES",
    "TASK_ATTEMPT_CONTRACTS",
    "attempt_contract_refs",
]
