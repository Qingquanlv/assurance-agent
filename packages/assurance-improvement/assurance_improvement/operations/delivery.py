"""Improvement delivery load, evaluate, apply, rollback, and export handlers."""

from __future__ import annotations

from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from graph_engine.plugin_api import EffectIntent, TaskContext, TaskOutcome, TaskRequest

from assurance_improvement.contracts.delivery import (
    ChangeExportReceipt,
    ImprovementDeliveryDocument,
    KnowledgeExportReceipt,
    MemoryApplyReceipt,
    MemoryEvalReceipt,
    MemoryRollbackReceipt,
)
from assurance_improvement.contracts.effects import ImprovementEffectIntentV1
from assurance_improvement.contracts.improvements import (
    DeliveryKind,
    ImprovementProjection,
    ImprovementState,
)
from assurance_improvement.contracts.promotion import PromotionReceipt, TestPromotionManifest
from assurance_improvement.operations.common import (
    InputError,
    as_json,
    failed_input,
    succeeded,
    validate_input,
)
from assurance_improvement.operations.keys import promotion_effect_key
from assurance_improvement.operations.review import assert_improvement_transition

_FROZEN = ConfigDict(frozen=True, extra="forbid")
DELIVERY_EFFECT = "assurance.improvement.effect.delivery.v1"
PROMOTION_EFFECT = "assurance.improvement.effect.promotion.v1"

_STAGE_RECEIPTS: dict[str, tuple[str, ...]] = {
    "evaluate": ("memory_eval",),
    "apply": ("memory_eval", "memory_apply"),
    "rollback": ("memory_rollback",),
    "export_change": ("change_export",),
    "export_knowledge": ("knowledge_export",),
}


class LoadDeliveryInput(BaseModel):
    model_config = _FROZEN

    projection: ImprovementProjection
    stage: Literal["evaluate", "apply", "rollback", "export_change", "export_knowledge"]
    change_export: ChangeExportReceipt | None = None
    knowledge_export: KnowledgeExportReceipt | None = None
    memory_eval: MemoryEvalReceipt | None = None
    memory_apply: MemoryApplyReceipt | None = None
    memory_rollback: MemoryRollbackReceipt | None = None


class EvaluateMemoryInput(BaseModel):
    model_config = _FROZEN

    projection: ImprovementProjection
    eval_run_id: str = Field(min_length=1)
    outcome: Literal["passed", "regressed", "awaiting_baseline", "error"]
    report_sha256: str = Field(min_length=1)
    staged_sha256: str = Field(min_length=1)
    baseline_sha256: str | None = None
    target_digest: str = Field(min_length=1)


class ApplyMemoryInput(BaseModel):
    model_config = _FROZEN

    projection: ImprovementProjection
    eval_receipt: MemoryEvalReceipt
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


class ExportKnowledgeInput(BaseModel):
    model_config = _FROZEN

    projection: ImprovementProjection
    artifact_path: str = Field(min_length=1)
    sha256: str = Field(min_length=1)
    created: bool
    target_digest: str = Field(min_length=1)


class RecordAppliedInput(BaseModel):
    model_config = _FROZEN

    projection: ImprovementProjection
    next_state: Literal["applied", "exported"]


class PromotionInput(BaseModel):
    model_config = _FROZEN

    projection: ImprovementProjection
    manifest: TestPromotionManifest
    receipt: PromotionReceipt
    promotion_digest: str = Field(min_length=1)


def _present_receipts(payload: LoadDeliveryInput) -> tuple[str, ...]:
    return tuple(
        name
        for name, value in (
            ("change_export", payload.change_export),
            ("knowledge_export", payload.knowledge_export),
            ("memory_eval", payload.memory_eval),
            ("memory_apply", payload.memory_apply),
            ("memory_rollback", payload.memory_rollback),
        )
        if value is not None
    )


def _delivery_intent(
    *,
    kind: str,
    projection: ImprovementProjection,
    target_kind: str,
    target_digest: str,
    target: str | None = None,
    artifact_path: str | None = None,
    reason: str | None = None,
) -> EffectIntent:
    payload = ImprovementEffectIntentV1.model_validate(
        {
            "schema_version": "1",
            "kind": kind,
            "improvement_id": projection.improvement_id,
            "version": projection.version,
            "target_kind": target_kind,
            "target_digest": target_digest,
            "target": target,
            "artifact_path": artifact_path,
            "reason": reason,
        }
    )
    return EffectIntent(
        kind=DELIVERY_EFFECT,
        payload=as_json(payload.model_dump(mode="json")),
    )


class LoadImprovementDeliveryHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            payload = validate_input(LoadDeliveryInput, request.input)
            if payload.projection.state is not ImprovementState.APPROVED:
                raise InputError("delivery requires an approved improvement")
            present = _present_receipts(payload)
            expected = _STAGE_RECEIPTS[payload.stage]
            if present != expected:
                raise InputError(f"{payload.stage} delivery requires exact receipts {expected}")
            document = ImprovementDeliveryDocument.model_validate(
                {
                    "schema_version": "1",
                    "improvement_id": payload.projection.improvement_id,
                    "expected_improvement_version": payload.projection.version,
                    "delivery": payload.projection.delivery.value,
                    "change_export": payload.change_export.model_dump(mode="json")
                    if payload.change_export
                    else None,
                    "knowledge_export": payload.knowledge_export.model_dump(mode="json")
                    if payload.knowledge_export
                    else None,
                    "memory_eval": payload.memory_eval.model_dump(mode="json")
                    if payload.memory_eval
                    else None,
                    "memory_apply": payload.memory_apply.model_dump(mode="json")
                    if payload.memory_apply
                    else None,
                    "memory_rollback": payload.memory_rollback.model_dump(mode="json")
                    if payload.memory_rollback
                    else None,
                }
            )
            return succeeded(cast(dict[str, object], document.model_dump(mode="json")))
        except InputError as error:
            return failed_input(error)


class EvaluateMemoryImprovementHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            payload = validate_input(EvaluateMemoryInput, request.input)
            if payload.projection.delivery is not DeliveryKind.MEMORY_PATCH:
                raise InputError("evaluate-memory requires memory_patch delivery")
            receipt = MemoryEvalReceipt(
                eval_run_id=payload.eval_run_id,
                outcome=payload.outcome,
                report_sha256=payload.report_sha256,
                staged_sha256=payload.staged_sha256,
                baseline_sha256=payload.baseline_sha256,
            )
            intent = _delivery_intent(
                kind="memory_eval",
                projection=payload.projection,
                target_kind="memory_eval",
                target_digest=payload.target_digest,
                target=payload.projection.target,
            )
            return succeeded(cast(dict[str, object], receipt.model_dump(mode="json")), effects=(intent,))
        except InputError as error:
            return failed_input(error)


class ApplyMemoryImprovementHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            payload = validate_input(ApplyMemoryInput, request.input)
            if payload.eval_receipt.outcome != "passed":
                raise InputError("memory apply requires a passed evaluation")
            receipt = MemoryApplyReceipt(
                target=payload.projection.target,
                before_sha256=payload.before_sha256,
                after_sha256=payload.after_sha256,
                receipt_sha256=payload.receipt_sha256,
            )
            intent = _delivery_intent(
                kind="memory_apply",
                projection=payload.projection,
                target_kind="memory_apply",
                target_digest=payload.target_digest,
                target=payload.projection.target,
            )
            return succeeded(cast(dict[str, object], receipt.model_dump(mode="json")), effects=(intent,))
        except InputError as error:
            return failed_input(error)


class RollbackMemoryImprovementHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            payload = validate_input(RollbackMemoryInput, request.input)
            receipt = MemoryRollbackReceipt(
                target=payload.projection.target,
                restored_sha256=payload.restored_sha256,
                reason=payload.reason,
            )
            intent = _delivery_intent(
                kind="memory_rollback",
                projection=payload.projection,
                target_kind="memory_rollback",
                target_digest=payload.target_digest,
                target=payload.projection.target,
                reason=payload.reason,
            )
            return succeeded(cast(dict[str, object], receipt.model_dump(mode="json")), effects=(intent,))
        except InputError as error:
            return failed_input(error)


class ExportChangeImprovementHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            payload = validate_input(ExportChangeInput, request.input)
            receipt = ChangeExportReceipt(
                sha256=payload.sha256,
                created=payload.created,
                artifact_path=payload.artifact_path,
            )
            intent = _delivery_intent(
                kind="change_export",
                projection=payload.projection,
                target_kind="change_export",
                target_digest=payload.target_digest,
                artifact_path=payload.artifact_path,
            )
            return succeeded(cast(dict[str, object], receipt.model_dump(mode="json")), effects=(intent,))
        except InputError as error:
            return failed_input(error)


class RecordChangeImprovementAppliedHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            payload = validate_input(RecordAppliedInput, request.input)
            target = (
                ImprovementState.APPLIED if payload.next_state == "applied" else ImprovementState.EXPORTED
            )
            assert_improvement_transition(payload.projection.state, target)
            updated = payload.projection.model_copy(
                update={"state": target, "version": payload.projection.version + 1}
            )
            return succeeded(cast(dict[str, object], updated.model_dump(mode="json")))
        except InputError as error:
            return failed_input(error)


class ExportKnowledgeImprovementHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            payload = validate_input(ExportKnowledgeInput, request.input)
            receipt = KnowledgeExportReceipt(
                sha256=payload.sha256,
                created=payload.created,
                artifact_path=payload.artifact_path,
            )
            intent = _delivery_intent(
                kind="knowledge_export",
                projection=payload.projection,
                target_kind="knowledge_export",
                target_digest=payload.target_digest,
                artifact_path=payload.artifact_path,
            )
            return succeeded(cast(dict[str, object], receipt.model_dump(mode="json")), effects=(intent,))
        except InputError as error:
            return failed_input(error)


class RecordKnowledgeImprovementAppliedHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            payload = validate_input(RecordAppliedInput, request.input)
            target = (
                ImprovementState.APPLIED if payload.next_state == "applied" else ImprovementState.EXPORTED
            )
            assert_improvement_transition(payload.projection.state, target)
            updated = payload.projection.model_copy(
                update={"state": target, "version": payload.projection.version + 1}
            )
            return succeeded(cast(dict[str, object], updated.model_dump(mode="json")))
        except InputError as error:
            return failed_input(error)


class ApplyTestPromotionHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            payload = validate_input(PromotionInput, request.input)
            if payload.manifest.improvement_id != payload.projection.improvement_id:
                raise InputError("promotion manifest improvement_id does not match")
            if payload.receipt.improvement_id != payload.projection.improvement_id:
                raise InputError("promotion receipt improvement_id does not match")
            intent_payload = ImprovementEffectIntentV1.model_validate(
                {
                    "schema_version": "1",
                    "kind": "test_promotion",
                    "improvement_id": payload.projection.improvement_id,
                    "version": payload.projection.version,
                    "promotion_digest": payload.promotion_digest,
                    "candidate_id": payload.manifest.candidate_id,
                }
            )
            key = promotion_effect_key(intent_payload)
            del key
            return succeeded(
                cast(dict[str, object], payload.receipt.model_dump(mode="json")),
                effects=(
                    EffectIntent(
                        kind=PROMOTION_EFFECT,
                        payload=as_json(intent_payload.model_dump(mode="json")),
                    ),
                ),
            )
        except InputError as error:
            return failed_input(error)


__all__ = [
    "ApplyMemoryImprovementHandler",
    "ApplyTestPromotionHandler",
    "EvaluateMemoryImprovementHandler",
    "ExportChangeImprovementHandler",
    "ExportKnowledgeImprovementHandler",
    "LoadImprovementDeliveryHandler",
    "RecordChangeImprovementAppliedHandler",
    "RecordKnowledgeImprovementAppliedHandler",
    "RollbackMemoryImprovementHandler",
]
