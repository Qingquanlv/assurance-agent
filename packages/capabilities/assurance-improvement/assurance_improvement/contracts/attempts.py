from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, cast

from agent_runtime_contracts import AgentExecutionContract
from graph_engine.attempts import (
    AttemptExecutionContext,
    AttemptRetryPolicy,
    AttemptTimeoutPolicy,
    TaskAttemptContract,
)
from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.plugin_api import (
    AttemptContractRef,
    EffectIntent,
    InvocationMetadata,
    ResourceClaimTemplate,
    ResourceClaims,
    TaskContext,
    TaskRequest,
    TaskWorkspaceIdentity,
)
from pydantic import BaseModel, ValidationError

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
from assurance_improvement.operations.retro import (
    ReconcileInput,
    RetroCollectInput,
    analysis_slice,
    assert_collect_identity,
)
from assurance_improvement.operations.review import ApplyAutoReviewInput, ApplyReviewInput
from assurance_improvement.contracts.retro import (
    EvalEvidenceSlice,
    IssueEvidenceSlice,
    WorkflowEvidenceSlice,
)

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


_SHA = "0" * 64


def select_retro_collect(payload: Mapping[str, object] | RetroCollectInput) -> RetroCollectInput:
    collected = (
        payload if isinstance(payload, RetroCollectInput) else RetroCollectInput.model_validate(payload)
    )
    assert_collect_identity(collected)
    return collected


def select_analysis_slice(
    collected: RetroCollectInput,
    *,
    domain: Literal["issue", "workflow", "eval"],
) -> IssueEvidenceSlice | WorkflowEvidenceSlice | EvalEvidenceSlice:
    if not isinstance(collected, RetroCollectInput):
        raise ValueError("analysis requires authenticated collect output")
    return analysis_slice(collected, domain)


def select_retro_reconcile(
    *,
    context: object,
    candidates: Sequence[object] = (),
    current: object,
    ts: str,
) -> ReconcileInput:
    return ReconcileInput.model_validate(
        {
            "context": context.model_dump(mode="json") if isinstance(context, BaseModel) else context,
            "candidates": [
                item.model_dump(mode="json") if isinstance(item, BaseModel) else item for item in candidates
            ],
            "current": current.model_dump(mode="json") if isinstance(current, BaseModel) else current,
            "ts": ts,
        }
    )


def select_retro_agent(ledger: ImprovementLedgerProjection) -> ImprovementLedgerProjection:
    return ImprovementLedgerProjection.model_validate(ledger.model_dump(mode="json"))


def select_evaluate_memory(payload: Mapping[str, object] | EvaluateMemoryInput) -> EvaluateMemoryInput:
    if isinstance(payload, EvaluateMemoryInput):
        return payload
    return EvaluateMemoryInput.model_validate(payload)


class ClosedImprovementExecutor:
    def __init__(self, handler_id: str, handler: object, output_model: type[BaseModel]) -> None:
        self.handler_id = handler_id
        self._handler = handler
        self._output_model = output_model
        self._effects: tuple[EffectIntent, ...] = ()
        self.dispatch_count = 0

    async def execute(self, validated_input: BaseModel, context: AttemptExecutionContext) -> BaseModel:
        from assurance_improvement.operations.common import InputError

        self.dispatch_count += 1
        request = _synthetic_request(validated_input, context, self.handler_id)
        outcome = await self._handler.execute(request, _synthetic_context(context))  # type: ignore[attr-defined]
        if outcome.failure is not None:
            raise InputError(outcome.failure.message)
        self._effects = tuple(outcome.effects)
        return _coerce_output(self._output_model, outcome.output)

    def declared_effects(self, output: BaseModel) -> tuple[EffectIntent, ...]:
        del output
        return self._effects


def close_improvement_task(handler_id: str) -> ClosedImprovementExecutor:
    from assurance_improvement.operations import improvement_handlers

    contract = TASK_ATTEMPT_CONTRACTS[handler_id]
    return ClosedImprovementExecutor(
        handler_id,
        improvement_handlers()[handler_id],
        contract.output_model,
    )


def _synthetic_request(
    validated_input: BaseModel,
    context: AttemptExecutionContext,
    handler_id: str,
) -> TaskRequest:
    invocation = InvocationMetadata(
        invocation_id=context.invocation_id,
        lock_digest=_SHA,
        composition_digest=_SHA,
        entrypoint=context.public_entrypoint,
    )
    return TaskRequest(
        invocation_id=context.invocation_id,
        task_id=context.attempt_key.digest,
        graph_instance_id=context.invocation_id,
        node_id=context.semantic_node_id,
        capability_id=handler_id,
        invocation=invocation,
        attempt=1,
        input=cast(JSONValue, validated_input.model_dump(mode="json")),
    )


def _synthetic_context(context: AttemptExecutionContext) -> TaskContext:
    invocation = InvocationMetadata(
        invocation_id=context.invocation_id,
        lock_digest=_SHA,
        composition_digest=_SHA,
        entrypoint=context.public_entrypoint,
    )
    identity_payload = {
        "task_id": context.attempt_key.digest,
        "attempt": 1,
        "attempt_id": "attempt-1",
        "output_paths": [],
        "baseline_files": [],
        "project_digest": _SHA,
        "write_root_digest": _SHA,
        "layout_schema_version": "1",
    }
    identity = TaskWorkspaceIdentity(
        **identity_payload,
        identity_digest=canonical_digest(cast(JSONValue, identity_payload)),
    )
    return TaskContext(
        project_root=Path("."),
        write_root=Path("."),
        workspace_identity=identity,
        heartbeat=lambda: None,
        cancel_requested=lambda: False,
        invocation=invocation,
    )


def _coerce_output(model: type[BaseModel], payload: object) -> BaseModel:
    if payload is None:
        raise ValueError("handler returned empty output")
    try:
        return model.model_validate(payload)
    except ValidationError:
        if isinstance(payload, Mapping):
            for key in ("status", "projection"):
                nested = payload.get(key)
                if nested is not None:
                    try:
                        return model.model_validate(nested)
                    except ValidationError:
                        continue
        raise


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
    "ClosedImprovementExecutor",
    "OUTPUT_ROUTE_TEMPLATES",
    "TASK_ATTEMPT_CONTRACTS",
    "attempt_contract_refs",
    "close_improvement_task",
    "select_analysis_slice",
    "select_evaluate_memory",
    "select_retro_agent",
    "select_retro_collect",
    "select_retro_reconcile",
]
