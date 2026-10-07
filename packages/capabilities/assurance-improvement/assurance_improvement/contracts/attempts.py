from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, cast

from agent_runtime_contracts import AgentExecutionContract
from agent_runtime_contracts.qa_paths import qa_route
from graph_engine.attempts import (
    RUNTIME_EVIDENCE,
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
from graph_engine.stategraph.ledger import InputBinding, NamedWrite
from pydantic import BaseModel

from assurance_improvement.contracts.delivery import (
    ChangeExportPublishedV1,
    MemoryApplyReceipt,
    MemoryEvalPublishedV1,
    MemoryRollbackPublishedV1,
)
from assurance_improvement.contracts.improvements import ImprovementLedgerProjection
from assurance_improvement.contracts.review import AppliedAutoReviewV1, ApplyReviewPublishedV1
from assurance_improvement.contracts.runtime_snapshot import (
    RUNTIME_EVIDENCE_ROOT,
    RetroRuntimeSnapshotInputV1,
    RetroRuntimeSnapshotOutputV1,
)
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
from assurance_improvement.contracts.handoff import (
    CANDIDATES,
    COLLECTED,
    CONTEXT,
    COVERAGE_GAP_SLICE,
    DISCOVERY_SLICE,
    EVAL_SLICE,
    ISSUE_SLICE,
    SLICE_WRITES,
    WORKFLOW_SLICE,
    COLLECTED_WRITE,
    CONTEXT_WRITE,
    MEMORY_EVAL_WRITE,
    MEMORY_APPLY_WRITE,
    MEMORY_ROLLBACK_WRITE,
    CHANGE_EXPORT_WRITE,
    PROJECTION_WRITE,
)
from assurance_improvement.contracts.retro import (
    EvalEvidenceSlice,
    IssueEvidenceSlice,
    RetroBuildSlicesInputV1,
    RetroCollectAttemptInput,
    RetroCollectInput,
    RetroCollectedV1,
    RetroReconcileAttemptInput,
    RetroReconcileResultV1,
    RetroSynthesizeAttemptInput,
    RetroSynthesizeV1,
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
    *,
    writes: tuple[NamedWrite, ...] = (),
    bindings: tuple[InputBinding, ...] = (),
    resources: ResourceClaims | None = None,
    capabilities: tuple[str, ...] = (),
) -> TaskAttemptContract[Any, Any]:
    suffix = handler_id.removeprefix("assurance.improvement.")
    claimed = resources
    if claimed is None:
        claimed = ResourceClaims(writes=tuple(item.root for item in writes))
    return TaskAttemptContract(
        contract_id=f"assurance.improvement.task.{suffix}",
        owner_id="assurance.improvement",
        handler_id=handler_id,
        input_model=input_model,
        output_model=output_model,
        resources=claimed,
        retry=_TASK_RETRY,
        timeout=_TIMEOUT,
        validators=(),
        writes=writes,
        bindings=bindings,
        capabilities=capabilities,
    )


def _bind(producer: TaskAttemptContract[Any, Any], name: str, field: str) -> InputBinding:
    handle = producer.artifact(name, slot=field)
    return InputBinding(ledger_key=handle.ledger_key, field=field, many=handle.many)


_BUILD_SLICES = TaskAttemptContract(
    contract_id="assurance.improvement.retro-build-slices",
    owner_id="assurance.improvement",
    handler_id="assurance.improvement.retro-build-slices.execute",
    input_model=RetroBuildSlicesInputV1,
    output_model=RetroCollectInput,
    resources=ResourceClaims(
        reads=("issues", "qa"),
        writes=(ISSUE_SLICE, WORKFLOW_SLICE, EVAL_SLICE, DISCOVERY_SLICE, COVERAGE_GAP_SLICE),
    ),
    retry=_TASK_RETRY,
    timeout=_TIMEOUT,
    validators=(),
    writes=SLICE_WRITES,
)
_SLICE_BINDINGS = (
    _bind(_BUILD_SLICES, "issue", "issue_slice_ref"),
    _bind(_BUILD_SLICES, "workflow", "workflow_slice_ref"),
    _bind(_BUILD_SLICES, "eval", "eval_slice_ref"),
    _bind(_BUILD_SLICES, "discovery", "discovery_slice_ref"),
    _bind(_BUILD_SLICES, "coverage_gap", "coverage_gap_slice_ref"),
)
_COLLECT = _task(
    "assurance.improvement.retro-collect-v3",
    RetroCollectAttemptInput,
    RetroCollectedV1,
    writes=(COLLECTED_WRITE,),
    bindings=_SLICE_BINDINGS,
    resources=ResourceClaims(reads=("qa",), writes=(COLLECTED,)),
)
_SYNTHESIZE = _task(
    "assurance.improvement.retro-synthesize",
    RetroSynthesizeAttemptInput,
    RetroSynthesizeV1,
    writes=(CONTEXT_WRITE,),
    bindings=(
        *_SLICE_BINDINGS,
        _bind(_COLLECT, "collected", "generated_at_ref"),
    ),
    resources=ResourceClaims(reads=("qa",), writes=(CONTEXT,)),
)
_RECONCILE = TaskAttemptContract(
    contract_id="assurance.improvement.task.reconcile-improvements",
    owner_id="assurance.improvement",
    handler_id="assurance.improvement.reconcile-improvements",
    input_model=RetroReconcileAttemptInput,
    output_model=RetroReconcileResultV1,
    resources=ResourceClaims(
        reads=("qa/improvements/ledger.json", CONTEXT, CANDIDATES),
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
    bindings=(_bind(_SYNTHESIZE, "context", "context_ref"),),
)
_AUTO_REVIEW = _task(
    "assurance.improvement.apply-improvement-auto-review",
    ApplyAutoReviewInput,
    AppliedAutoReviewV1,
    writes=(PROJECTION_WRITE,),
)
_HUMAN_REVIEW = _task(
    "assurance.improvement.apply-improvement-review",
    ApplyReviewInput,
    ApplyReviewPublishedV1,
    writes=(PROJECTION_WRITE,),
    bindings=(_bind(_AUTO_REVIEW, "projection", "projection_ref"),),
)
_EVALUATE = _task(
    "assurance.improvement.evaluate-memory-improvement",
    EvaluateMemoryInput,
    MemoryEvalPublishedV1,
    writes=(MEMORY_EVAL_WRITE,),
    bindings=(_bind(_AUTO_REVIEW, "projection", "projection_ref"),),
)
_RUNTIME_SNAPSHOT = _task(
    "assurance.improvement.retro-runtime-snapshot",
    RetroRuntimeSnapshotInputV1,
    RetroRuntimeSnapshotOutputV1,
    writes=(NamedWrite("runtime-evidence", RUNTIME_EVIDENCE_ROOT, many=True),),
    resources=ResourceClaims(reads=("qa",), writes=(RUNTIME_EVIDENCE_ROOT,)),
    capabilities=(RUNTIME_EVIDENCE,),
)
_APPLY = _task(
    "assurance.improvement.apply-memory-improvement",
    ApplyMemoryInput,
    MemoryApplyReceipt,
    writes=(MEMORY_APPLY_WRITE,),
    bindings=(
        _bind(_AUTO_REVIEW, "projection", "projection_ref"),
        _bind(_EVALUATE, "memory-eval", "eval_receipt_ref"),
    ),
)

TASK_ATTEMPT_CONTRACTS: Mapping[str, TaskAttemptContract[Any, Any]] = MappingProxyType(
    {
        "assurance.improvement.retro-build-slices": _BUILD_SLICES,
        "assurance.improvement.apply-improvement-auto-review": _AUTO_REVIEW,
        "assurance.improvement.apply-improvement-review": _HUMAN_REVIEW,
        "assurance.improvement.apply-memory-improvement": _APPLY,
        "assurance.improvement.evaluate-memory-improvement": _EVALUATE,
        "assurance.improvement.export-change-improvement": _task(
            "assurance.improvement.export-change-improvement",
            ExportChangeInput,
            ChangeExportPublishedV1,
            writes=(CHANGE_EXPORT_WRITE,),
        ),
        "assurance.improvement.reconcile-improvements": _RECONCILE,
        "assurance.improvement.retro-synthesize": _SYNTHESIZE,
        "assurance.improvement.retro-collect-v3": _COLLECT,
        "assurance.improvement.retro-runtime-snapshot": _RUNTIME_SNAPSHOT,
        "assurance.improvement.rollback-memory-improvement": _task(
            "assurance.improvement.rollback-memory-improvement",
            RollbackMemoryInput,
            MemoryRollbackPublishedV1,
            writes=(MEMORY_ROLLBACK_WRITE,),
        ),
    }
)

AGENT_JOB_CONTRACTS, OUTPUT_ROUTE_TEMPLATES = _agent_catalog()

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
        project_root = scope.workspace.project_root if isinstance(scope, Scope) else Path(".")
        write_root = scope.workspace.write_root if isinstance(scope, Scope) else Path(".")
        request = _synthetic_request(validated_input, context, self.handler_id)
        outcome = await self._handler.execute(  # type: ignore[attr-defined]
            request, _synthetic_context(context, project_root=project_root, write_root=write_root)
        )
        if outcome.failure is not None:
            raise InputError(outcome.failure.message)
        return ExecutedAttemptResult(
            output=self._output_model.model_validate(outcome.output),
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


def _synthetic_context(
    context: AttemptExecutionContext,
    *,
    project_root: Path = Path("."),
    write_root: Path = Path("."),
) -> TaskContext:
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
        project_root=project_root,
        write_root=write_root,
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
