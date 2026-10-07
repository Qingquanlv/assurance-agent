"""Improvement delivery load, evaluate, apply, rollback, and export handlers."""

from __future__ import annotations

from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from agent_runtime_contracts.ops import InputError, failed_input
from assurance_intake.contracts import EvidenceArtifactRefV1
from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest

from assurance_improvement.contracts.delivery import (
    ChangeExportPublishedV1,
    ChangeExportReceipt,
    ChangeExportRecordV1,
    ImprovementApplyProof,
    MemoryApplyReceipt,
    MemoryApplyRecordV1,
    MemoryEvalPublishedV1,
    MemoryEvalReceipt,
    MemoryEvalRecordV1,
    MemoryRollbackPublishedV1,
    MemoryRollbackReceipt,
    MemoryRollbackRecordV1,
    artifact_digest,
    evaluate_apply_route,
    same_digest,
)
from assurance_improvement.contracts.improvements import (
    DeliveryKind,
    ImprovementProjection,
    ImprovementState,
)
from assurance_improvement.operations.common import succeeded, validate_input

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class EvaluateMemoryInput(BaseModel):
    model_config = _FROZEN

    projection: ImprovementProjection | None = None
    projection_ref: EvidenceArtifactRefV1 | None = None
    eval_run_id: str = Field(min_length=1)
    outcome: Literal["passed", "regressed", "awaiting_baseline", "error"]
    report_sha256: str = Field(min_length=1)
    staged_sha256: str = Field(min_length=1)
    baseline_sha256: str | None = None
    target_digest: str = Field(min_length=1)


class ApplyMemoryInput(BaseModel):
    model_config = _FROZEN

    projection: ImprovementProjection | None = None
    projection_ref: EvidenceArtifactRefV1 | None = None
    eval_receipt: MemoryEvalReceipt | None = None
    eval_receipt_ref: EvidenceArtifactRefV1 | None = None
    approved_state_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    approved_version: int = Field(ge=1)
    before_sha256: str = Field(min_length=1)
    after_sha256: str = Field(min_length=1)
    receipt_sha256: str = Field(min_length=1)
    target_digest: str = Field(min_length=1)


class RollbackMemoryInput(BaseModel):
    model_config = _FROZEN

    projection: ImprovementProjection
    reason: str = Field(min_length=1)
    restored_sha256: str = Field(min_length=1)
    target_digest: str = Field(min_length=1)


class ExportChangeInput(BaseModel):
    model_config = _FROZEN

    projection: ImprovementProjection
    artifact_path: str = Field(min_length=1)
    sha256: str = Field(min_length=1)
    created: bool
    target_digest: str = Field(min_length=1)


def assert_delivery_gate(projection: ImprovementProjection, kind: DeliveryKind) -> None:
    if projection.state is not ImprovementState.APPROVED:
        raise InputError("delivery requires an approved improvement")
    if projection.delivery is not kind:
        raise InputError(f"delivery requires {kind.value}")
    if not projection.target:
        raise InputError("delivery requires an exact target")


def assert_authenticated_approval(projection: ImprovementProjection) -> None:
    assert_delivery_gate(projection, projection.delivery)
    if projection.approval_source not in {"human", "automatic"}:
        raise InputError("apply requires authenticated approval")
    if projection.approval_source == "automatic" and projection.last_auto_review is None:
        raise InputError("apply requires an authenticated auto-review projection")


def assert_apply_proof(
    projection: ImprovementProjection,
    eval_receipt: MemoryEvalReceipt,
    *,
    approved_state_digest: str,
    approved_version: int,
) -> ImprovementApplyProof:
    assert_authenticated_approval(projection)
    if approved_version != projection.version:
        raise InputError("apply approved version is stale")
    current_digest = artifact_digest(projection)
    if not same_digest(approved_state_digest, current_digest):
        raise InputError("apply approved state digest does not match")
    try:
        return ImprovementApplyProof(
            approved_state_digest=current_digest,
            approved_version=projection.version,
            evaluation=eval_receipt,
        )
    except ValueError as error:
        raise InputError(str(error)) from error


def _bound_projection(
    payload: EvaluateMemoryInput | ApplyMemoryInput, context: TaskContext
) -> ImprovementProjection:
    from assurance_improvement.operations.files import load_named

    if payload.projection_ref is not None:
        return load_named(context, payload.projection_ref, ImprovementProjection)
    if payload.projection is not None:
        return payload.projection
    raise InputError("improvement projection is missing")


def evaluate_memory(payload: EvaluateMemoryInput) -> MemoryEvalRecordV1:
    if payload.projection is None:
        raise InputError("improvement projection is missing")
    assert_delivery_gate(payload.projection, DeliveryKind.MEMORY_PATCH)
    receipt = MemoryEvalReceipt(
        eval_run_id=payload.eval_run_id,
        outcome=payload.outcome,
        report_sha256=payload.report_sha256,
        staged_sha256=payload.staged_sha256,
        baseline_sha256=payload.baseline_sha256,
        approved_state_digest=artifact_digest(payload.projection),
        approved_version=payload.projection.version,
    )
    return MemoryEvalRecordV1(
        target=payload.projection.target,
        improvement_id=payload.projection.improvement_id,
        version=payload.projection.version,
        target_digest=payload.target_digest,
        receipt=receipt,
    )


class EvaluateMemoryImprovementHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        from assurance_improvement.contracts.handoff import MEMORY_EVAL
        from assurance_improvement.operations.files import stage_named

        try:
            payload = validate_input(EvaluateMemoryInput, request.input)
            record = evaluate_memory(
                payload.model_copy(update={"projection": _bound_projection(payload, context)})
            )
            stage_named(context, MEMORY_EVAL, record)
            published = MemoryEvalPublishedV1(
                memory_eval=record.receipt, route=evaluate_apply_route(record.receipt.outcome)
            )
            return succeeded(cast(dict[str, object], published.model_dump(mode="json")))
        except InputError as error:
            return failed_input(error)


class ApplyMemoryImprovementHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            payload = validate_input(ApplyMemoryInput, request.input)
            projection = _bound_projection(payload, context)
            if payload.eval_receipt_ref is not None:
                from assurance_improvement.operations.files import load_named

                eval_receipt = load_named(context, payload.eval_receipt_ref, MemoryEvalRecordV1).receipt
            elif payload.eval_receipt is not None:
                eval_receipt = payload.eval_receipt
            else:
                raise InputError("apply requires an eval receipt")
            assert_apply_proof(
                projection,
                eval_receipt,
                approved_state_digest=payload.approved_state_digest,
                approved_version=payload.approved_version,
            )
            receipt = MemoryApplyReceipt(
                target=projection.target,
                before_sha256=payload.before_sha256,
                after_sha256=payload.after_sha256,
                receipt_sha256=payload.receipt_sha256,
            )
            from assurance_improvement.contracts.handoff import MEMORY_APPLY
            from assurance_improvement.operations.files import stage_named

            stage_named(
                context,
                MEMORY_APPLY,
                MemoryApplyRecordV1(
                    improvement_id=projection.improvement_id,
                    version=projection.version,
                    target_digest=payload.target_digest,
                    receipt=receipt,
                ),
            )
            return succeeded(cast(dict[str, object], receipt.model_dump(mode="json")))
        except InputError as error:
            return failed_input(error)


class RollbackMemoryImprovementHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            payload = validate_input(RollbackMemoryInput, request.input)
            assert_delivery_gate(payload.projection, DeliveryKind.MEMORY_PATCH)
            receipt = MemoryRollbackReceipt(
                target=payload.projection.target,
                restored_sha256=payload.restored_sha256,
                reason=payload.reason,
            )
            published = MemoryRollbackPublishedV1(
                target=receipt.target,
                restored_sha256=receipt.restored_sha256,
                reason=receipt.reason,
            )
            from assurance_improvement.contracts.handoff import MEMORY_ROLLBACK
            from assurance_improvement.operations.files import stage_named

            stage_named(
                context,
                MEMORY_ROLLBACK,
                MemoryRollbackRecordV1(
                    improvement_id=payload.projection.improvement_id,
                    version=payload.projection.version,
                    target_digest=payload.target_digest,
                    receipt=receipt,
                ),
            )
            return succeeded(cast(dict[str, object], published.model_dump(mode="json")))
        except InputError as error:
            return failed_input(error)


class ExportChangeImprovementHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            payload = validate_input(ExportChangeInput, request.input)
            assert_delivery_gate(payload.projection, DeliveryKind.CHANGE_DRAFT)
            receipt = ChangeExportReceipt(
                sha256=payload.sha256,
                created=payload.created,
                artifact_path=payload.artifact_path,
            )
            published = ChangeExportPublishedV1(
                sha256=receipt.sha256,
                created=receipt.created,
                artifact_path=receipt.artifact_path,
            )
            from assurance_improvement.contracts.handoff import CHANGE_EXPORT
            from assurance_improvement.operations.files import stage_named

            stage_named(
                context,
                CHANGE_EXPORT,
                ChangeExportRecordV1(
                    improvement_id=payload.projection.improvement_id,
                    version=payload.projection.version,
                    target_digest=payload.target_digest,
                    receipt=receipt,
                ),
            )
            return succeeded(cast(dict[str, object], published.model_dump(mode="json")))
        except InputError as error:
            return failed_input(error)


__all__ = [
    "ApplyMemoryImprovementHandler",
    "EvaluateMemoryImprovementHandler",
    "ExportChangeImprovementHandler",
    "RollbackMemoryImprovementHandler",
    "assert_apply_proof",
    "assert_authenticated_approval",
    "evaluate_memory",
]
