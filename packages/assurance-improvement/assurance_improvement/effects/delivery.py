"""Improvement delivery durable effect, including explicit rollback."""

from __future__ import annotations

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


_RECEIPT_FIELDS = {
    "change_export": "change_export",
    "knowledge_export": "knowledge_export",
    "memory_eval": "memory_eval",
    "memory_apply": "memory_apply",
    "memory_rollback": "memory_rollback",
    "declaration_write": "declaration",
}


def _delivery_receipt(payload: ImprovementEffectIntentV1) -> dict[str, object]:
    field = _RECEIPT_FIELDS.get(payload.kind)
    if field is None:
        raise ValueError(f"delivery effect does not persist {payload.kind}")
    document = getattr(payload, field)
    if document is None:
        raise ValueError(f"delivery intent is missing the {payload.kind} receipt")
    receipt = ImprovementEffectReceiptV1.model_validate(
        {
            "schema_version": "1",
            "kind": payload.kind,
            "improvement_id": payload.improvement_id,
            field: document.model_dump(mode="json"),
        }
    )
    return receipt.model_dump(mode="json")
