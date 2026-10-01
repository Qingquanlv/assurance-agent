"""Improvement delivery load, evaluate, apply, rollback, and export handlers."""

from __future__ import annotations

from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from agent_runtime_contracts.ops import InputError, failed_input
from graph_engine.plugin_api import EffectIntent, TaskContext, TaskOutcome, TaskRequest

from assurance_improvement.contracts.delivery import (
    ApplyAttemptResult,
    ChangeExportReceipt,
    ImprovementApplyProof,
    ImprovementDeliveryDocument,
    KnowledgeExportReceipt,
    MemoryApplyReceipt,
    MemoryEvalReceipt,
    MemoryRollbackReceipt,
    artifact_digest,
    same_digest,
)
from assurance_improvement.contracts.effects import ImprovementEffectIntentV1
from assurance_improvement.contracts.improvements import (
    DeliveryKind,
    ImprovementProjection,
    ImprovementState,
)
from assurance_improvement.contracts.promotion import PromotionReceipt, TestPromotionManifest
from assurance_improvement.operations.common import as_json, succeeded, validate_input
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
_STAGE_KIND: dict[str, DeliveryKind] = {
    "evaluate": DeliveryKind.MEMORY_PATCH,
    "apply": DeliveryKind.MEMORY_PATCH,
    "rollback": DeliveryKind.MEMORY_PATCH,
    "export_change": DeliveryKind.CHANGE_DRAFT,
    "export_knowledge": DeliveryKind.KNOWLEDGE_DELTA,
}
_INTENT_RECEIPT_FIELD = {
    "change_export": "change_export",
    "knowledge_export": "knowledge_export",
    "memory_eval": "memory_eval",
    "memory_apply": "memory_apply",
    "memory_rollback": "memory_rollback",
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


_ATTEMPT_STATES: dict[str, ImprovementState] = {
    "proposed": ImprovementState.PROPOSED,
    "changes_requested": ImprovementState.NEEDS_REWORK,
    "needs_rework": ImprovementState.NEEDS_REWORK,
    "rejected": ImprovementState.REJECTED,
    "superseded": ImprovementState.SUPERSEDED,
    "approved": ImprovementState.APPROVED,
}


def attempt_apply(
    *,
    state: str,
    evaluation: str = "passed",
    forge_state_digest: bool = False,
) -> ApplyAttemptResult:
    try:
        if state not in _ATTEMPT_STATES:
            raise InputError(f"unsupported apply state: {state}")
        resolved = _ATTEMPT_STATES[state]
        projection = _attempt_projection(resolved)
        current_digest = artifact_digest(projection)
        receipt = MemoryEvalReceipt(
            eval_run_id="eval-1",
            outcome="regressed" if evaluation == "failed" else "passed",
            report_sha256="r",
            staged_sha256="s",
            baseline_sha256=None,
            approved_state_digest=None if evaluation == "missing" else current_digest,
            approved_version=None if evaluation == "missing" else projection.version,
        )
        if evaluation == "stale":
            receipt = receipt.model_copy(
                update={
                    "approved_state_digest": "sha256:" + ("d" * 64),
                    "approved_version": projection.version,
                }
            )
        digest = current_digest
        if forge_state_digest:
            digest = "sha256:" + ("0" * 64)
        intent = _apply_memory(
            projection=projection,
            eval_receipt=receipt,
            approved_state_digest=digest,
            approved_version=projection.version,
            before_sha256="b",
            after_sha256="a",
            receipt_sha256="r",
            target_digest="a" * 64,
        )
        return ApplyAttemptResult(
            applied=True,
            effect_intents=(intent,),
            write_authorization=(projection.target,),
        )
    except InputError:
        return ApplyAttemptResult(applied=False, effect_intents=(), write_authorization=())


def _attempt_projection(state: ImprovementState) -> ImprovementProjection:
    payload: dict[str, object] = {
        "improvement_id": "IMP-1",
        "fingerprint": "f" * 64,
        "kind": "prompt_improvement",
        "delivery": "memory_patch",
        "source_refs": {"problem_ids": ["PROB-1"], "occurrence_ids": ["OCC-1"]},
        "target": ".aa/memory/aa-api-plan.md",
        "rationale": "gap",
        "proposed_change": "register adapters",
        "verification": {"suites": [], "required_cases": [], "success_criteria": "review"},
        "risk": "low",
        "confidence": "high",
        "state": state.value,
        "version": 1,
        "proposed_by_retro_ids": ["RET-1"],
        "last_event_id": "IMPEVT-1",
        "approval_source": "none",
    }
    if state is ImprovementState.APPROVED:
        payload["approval_source"] = "automatic"
        payload["last_auto_review"] = {
            "review_id": "REV-1",
            "subject_sha256": "sha256:" + ("a" * 64),
            "assessment_sha256": "sha256:" + ("b" * 64),
            "policy_version": "1",
            "verdict": "auto_approved",
        }
    return ImprovementProjection.model_validate(payload)


def _apply_memory(
    *,
    projection: ImprovementProjection,
    eval_receipt: MemoryEvalReceipt,
    approved_state_digest: str,
    approved_version: int,
    before_sha256: str,
    after_sha256: str,
    receipt_sha256: str,
    target_digest: str,
) -> EffectIntent:
    assert_apply_proof(
        projection,
        eval_receipt,
        approved_state_digest=approved_state_digest,
        approved_version=approved_version,
    )
    receipt = MemoryApplyReceipt(
        target=projection.target,
        before_sha256=before_sha256,
        after_sha256=after_sha256,
        receipt_sha256=receipt_sha256,
    )
    return _delivery_intent(
        kind="memory_apply",
        projection=projection,
        target_kind="memory_apply",
        target_digest=target_digest,
        target=projection.target,
        receipt=receipt,
    )


def _delivery_intent(
    *,
    kind: str,
    projection: ImprovementProjection,
    target_kind: str,
    target_digest: str,
    receipt: ChangeExportReceipt
    | KnowledgeExportReceipt
    | MemoryEvalReceipt
    | MemoryApplyReceipt
    | MemoryRollbackReceipt,
    target: str | None = None,
    artifact_path: str | None = None,
    reason: str | None = None,
) -> EffectIntent:
    field = _INTENT_RECEIPT_FIELD[kind]
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
            field: receipt.model_dump(mode="json"),
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
            assert_delivery_gate(payload.projection, _STAGE_KIND[payload.stage])
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


def evaluate_memory(payload: EvaluateMemoryInput) -> tuple[MemoryEvalReceipt, EffectIntent]:
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
    intent = _delivery_intent(
        kind="memory_eval",
        projection=payload.projection,
        target_kind="memory_eval",
        target_digest=payload.target_digest,
        target=payload.projection.target,
        receipt=receipt,
    )
    return receipt, intent


class EvaluateMemoryImprovementHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            receipt, intent = evaluate_memory(validate_input(EvaluateMemoryInput, request.input))
            return succeeded(cast(dict[str, object], receipt.model_dump(mode="json")), effects=(intent,))
        except InputError as error:
            return failed_input(error)


class ApplyMemoryImprovementHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            payload = validate_input(ApplyMemoryInput, request.input)
            intent = _apply_memory(
                projection=payload.projection,
                eval_receipt=payload.eval_receipt,
                approved_state_digest=payload.approved_state_digest,
                approved_version=payload.approved_version,
                before_sha256=payload.before_sha256,
                after_sha256=payload.after_sha256,
                receipt_sha256=payload.receipt_sha256,
                target_digest=payload.target_digest,
            )
            receipt = MemoryApplyReceipt(
                target=payload.projection.target,
                before_sha256=payload.before_sha256,
                after_sha256=payload.after_sha256,
                receipt_sha256=payload.receipt_sha256,
            )
            return succeeded(cast(dict[str, object], receipt.model_dump(mode="json")), effects=(intent,))
        except InputError as error:
            return failed_input(error)


class RollbackMemoryImprovementHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            payload = validate_input(RollbackMemoryInput, request.input)
            assert_delivery_gate(payload.projection, DeliveryKind.MEMORY_PATCH)
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
                receipt=receipt,
            )
            return succeeded(cast(dict[str, object], receipt.model_dump(mode="json")), effects=(intent,))
        except InputError as error:
            return failed_input(error)


class ExportChangeImprovementHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            payload = validate_input(ExportChangeInput, request.input)
            assert_delivery_gate(payload.projection, DeliveryKind.CHANGE_DRAFT)
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
                receipt=receipt,
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
            assert_delivery_gate(payload.projection, DeliveryKind.KNOWLEDGE_DELTA)
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
                receipt=receipt,
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
                    "promotion": payload.receipt.model_dump(mode="json"),
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
    "attempt_apply",
    "assert_apply_proof",
    "assert_authenticated_approval",
    "evaluate_memory",
]
