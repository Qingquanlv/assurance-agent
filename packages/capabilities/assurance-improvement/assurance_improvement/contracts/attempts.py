from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, cast

from agent_runtime_contracts import AgentExecutionContract
from agent_runtime_contracts.qa_paths import qa_route
from graph_engine.attempts import (
    AttemptExecutionContext,
    AttemptRetryPolicy,
    AttemptTimeoutPolicy,
    AuthorizedAttemptScope,
    ExecutedAttemptResult,
    TaskAttemptContract,
)
from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.plugin_api import (
    AttemptContractRef,
    InvocationMetadata,
    ResourceClaims,
    TaskContext,
    TaskRequest,
    TaskWorkspaceIdentity,
)
from pydantic import BaseModel

from assurance_improvement.contracts.delivery import (
    ChangeExportReceipt,
    MemoryApplyReceipt,
    MemoryEvalReceipt,
    MemoryRollbackReceipt,
)
from assurance_improvement.contracts.improvements import (
    ImprovementLedgerProjection,
    ImprovementProjection,
)
from assurance_improvement.contracts.review import AppliedAutoReviewV1
from assurance_improvement.operations.delivery import (
    ApplyMemoryInput,
    EvaluateMemoryInput,
    ExportChangeInput,
    RollbackMemoryInput,
)
from assurance_improvement.operations.retro import (
    ReconcileInput,
    analysis_slice,
    assert_collect_identity,
)
from assurance_improvement.operations.review import ApplyAutoReviewInput, ApplyReviewInput
from assurance_improvement.contracts.retro import (
    EvalEvidenceSlice,
    IssueEvidenceSlice,
    RetroBuildSlicesInputV1,
    RetroCollectInput,
    RetroCollectedV1,
    RetroReconcileInputV1,
    RetroReconcileResultV1,
    WorkflowEvidenceSlice,
)

_TASK_RETRY = AttemptRetryPolicy(max_attempts=1)
_TIMEOUT = AttemptTimeoutPolicy(seconds=60)


def _paths(*suffixes: str) -> tuple[str, ...]:
    return qa_route(*suffixes)


def _agent_catalog() -> tuple[Mapping[str, Any], Mapping[str, tuple[str, ...]]]:
    from assurance_improvement.ops import router

    return router.agent_contracts(), router.output_routes()


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
        retry=_TASK_RETRY,
        timeout=_TIMEOUT,
        validators=(),
    )


AGENT_JOB_CONTRACTS, OUTPUT_ROUTE_TEMPLATES = _agent_catalog()
TASK_ATTEMPT_CONTRACTS: Mapping[str, TaskAttemptContract[Any, Any]] = MappingProxyType(
    {
        "assurance.improvement.retro-build-slices": TaskAttemptContract(
            contract_id="assurance.improvement.retro-build-slices",
            owner_id="assurance.improvement",
            handler_id="assurance.improvement.retro-build-slices.execute",
            input_model=RetroBuildSlicesInputV1,
            output_model=RetroCollectInput,
            resources=ResourceClaims(reads=("issues", "qa")),
            retry=_TASK_RETRY,
            timeout=_TIMEOUT,
            validators=(),
        ),
        "assurance.improvement.apply-improvement-auto-review": _task(
            "assurance.improvement.apply-improvement-auto-review",
            ApplyAutoReviewInput,
            AppliedAutoReviewV1,
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
        "assurance.improvement.reconcile-improvements": TaskAttemptContract(
            contract_id="assurance.improvement.task.reconcile-improvements",
            owner_id="assurance.improvement",
            handler_id="assurance.improvement.reconcile-improvements",
            input_model=RetroReconcileInputV1,
            output_model=RetroReconcileResultV1,
            resources=ResourceClaims(
                reads=("qa/improvements/ledger.json",),
                writes=tuple(
                    sorted(
                        (
                            "qa/improvements/ledger.json",
                            *_paths(
                                "retro/context.json",
                                "retro/candidates.json",
                                "retro/reconciliation.json",
                                "retro/status.json",
                            ),
                        )
                    )
                ),
            ),
            retry=_TASK_RETRY,
            timeout=_TIMEOUT,
            validators=(),
        ),
        "assurance.improvement.retro-collect-v3": _task(
            "assurance.improvement.retro-collect-v3",
            RetroCollectInput,
            RetroCollectedV1,
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
        self.dispatch_count = 0

    async def execute(
        self,
        validated_input: BaseModel,
        scope: AuthorizedAttemptScope | AttemptExecutionContext,
    ) -> ExecutedAttemptResult[Any]:
        from agent_runtime_contracts.ops import InputError
        from graph_engine.attempts import AuthorizedAttemptScope as Scope

        self.dispatch_count += 1
        context = scope.execution if isinstance(scope, Scope) else scope
        request = _synthetic_request(validated_input, context, self.handler_id)
        outcome = await self._handler.execute(request, _synthetic_context(context))  # type: ignore[attr-defined]
        if outcome.failure is not None:
            raise InputError(outcome.failure.message)
        return ExecutedAttemptResult(
            output=self._output_model.model_validate(outcome.output),
            effects=tuple(outcome.effects),
        )


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
