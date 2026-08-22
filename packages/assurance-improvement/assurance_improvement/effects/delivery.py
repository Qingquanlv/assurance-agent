"""Improvement delivery durable effect, including explicit rollback."""

from __future__ import annotations

from assurance_improvement.contracts.delivery import (
    ChangeExportReceipt,
    KnowledgeExportReceipt,
    MemoryApplyReceipt,
    MemoryEvalReceipt,
    MemoryRollbackReceipt,
)
from assurance_improvement.contracts.declarations import DeclarationProposalReceipt
from assurance_improvement.contracts.effects import ImprovementEffectIntentV1, ImprovementEffectReceiptV1
from assurance_improvement.effects.common import apply_effect, reconcile_effect
from assurance_improvement.effects.store import ImprovementStore
from assurance_improvement.operations.keys import delivery_effect_key
from graph_engine.plugin_api import EffectApplyResult, EffectIntent, EffectReconcileResult

DELIVERY_KIND = "assurance.improvement.effect.delivery.v1"
DELIVERY_INTENT_SCHEMA = "assurance.improvement.schema.improvement-effect-intent.v1"
DELIVERY_RECEIPT_SCHEMA = "assurance.improvement.schema.improvement-effect-receipt.v1"


class ImprovementDeliveryEffect:
    def __init__(self, store: ImprovementStore) -> None:
        self._store = store

    async def apply(self, intent: EffectIntent, idempotency_key: str) -> EffectApplyResult:
        return await apply_effect(
            store=self._store,
            intent=intent,
            idempotency_key=idempotency_key,
            expected_kind=DELIVERY_KIND,
            intent_model=ImprovementEffectIntentV1,
            derived_key=delivery_effect_key,
            build_receipt=_delivery_receipt,
        )

    async def reconcile(self, intent: EffectIntent, idempotency_key: str) -> EffectReconcileResult:
        return await reconcile_effect(
            store=self._store,
            intent=intent,
            idempotency_key=idempotency_key,
            expected_kind=DELIVERY_KIND,
            intent_model=ImprovementEffectIntentV1,
            derived_key=delivery_effect_key,
        )


def _delivery_receipt(payload: ImprovementEffectIntentV1) -> dict[str, object]:
    target = payload.target or payload.artifact_path or payload.improvement_id
    digest = payload.target_digest or "sha256:" + ("0" * 64)
    kind_payloads: dict[str, object] = {
        "change_export": ChangeExportReceipt(
            sha256=digest, created=True, artifact_path=payload.artifact_path or target
        ).model_dump(mode="json")
        if payload.kind == "change_export"
        else None,
        "knowledge_export": KnowledgeExportReceipt(
            sha256=digest, created=True, artifact_path=payload.artifact_path or target
        ).model_dump(mode="json")
        if payload.kind == "knowledge_export"
        else None,
        "memory_eval": MemoryEvalReceipt(
            eval_run_id=payload.candidate_id or payload.improvement_id,
            outcome="passed",
            report_sha256=digest,
            staged_sha256=digest,
            baseline_sha256=None,
        ).model_dump(mode="json")
        if payload.kind == "memory_eval"
        else None,
        "memory_apply": MemoryApplyReceipt(
            target=target,
            before_sha256=digest,
            after_sha256=digest,
            receipt_sha256=digest,
        ).model_dump(mode="json")
        if payload.kind == "memory_apply"
        else None,
        "memory_rollback": MemoryRollbackReceipt(
            target=target,
            restored_sha256=digest,
            reason=payload.reason or "rollback",
        ).model_dump(mode="json")
        if payload.kind == "memory_rollback"
        else None,
        "declaration": DeclarationProposalReceipt(
            improvement_id=payload.improvement_id,
            path=payload.artifact_path or target,
            digest=digest,
            status="accepted",
        ).model_dump(mode="json")
        if payload.kind == "declaration_write"
        else None,
    }
    receipt = ImprovementEffectReceiptV1.model_validate(
        {
            "schema_version": "1",
            "kind": payload.kind,
            "improvement_id": payload.improvement_id,
            "change_export": kind_payloads["change_export"],
            "knowledge_export": kind_payloads["knowledge_export"],
            "memory_eval": kind_payloads["memory_eval"],
            "memory_apply": kind_payloads["memory_apply"],
            "memory_rollback": kind_payloads["memory_rollback"],
            "declaration": kind_payloads["declaration"],
        }
    )
    return receipt.model_dump(mode="json")
