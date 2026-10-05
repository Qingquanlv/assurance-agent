"""Improvement delivery durable effect, including explicit rollback."""

from __future__ import annotations

from assurance_improvement.contracts.effects import ImprovementEffectIntentV1, ImprovementEffectReceiptV1
from assurance_improvement.operations.keys import delivery_effect_key
from graph_engine.effects.idempotent import apply_idempotent_effect, reconcile_idempotent_effect
from graph_engine.effects.state import EffectCallContext
from graph_engine.plugin_api import EffectApplyResult, EffectIntent, EffectReconcileResult

DELIVERY_KIND = "assurance.improvement.effect.delivery.v1"
DELIVERY_INTENT_SCHEMA = "assurance.improvement.schema.improvement-effect-intent.v1"
DELIVERY_RECEIPT_SCHEMA = "assurance.improvement.schema.improvement-effect-receipt.v1"

_RECEIPT_FIELDS = {
    "change_export": "change_export",
    "knowledge_export": "knowledge_export",
    "memory_eval": "memory_eval",
    "memory_apply": "memory_apply",
    "memory_rollback": "memory_rollback",
    "declaration_write": "declaration",
}


class ImprovementDeliveryEffect:
    async def apply(self, intent: EffectIntent, context: EffectCallContext) -> EffectApplyResult:
        return await apply_idempotent_effect(
            context=context,
            intent=intent,
            expected_kind=DELIVERY_KIND,
            intent_model=ImprovementEffectIntentV1,
            derived_key=delivery_effect_key,
            build_receipt=_delivery_receipt,
        )

    async def reconcile(self, intent: EffectIntent, context: EffectCallContext) -> EffectReconcileResult:
        return await reconcile_idempotent_effect(
            context=context,
            intent=intent,
            expected_kind=DELIVERY_KIND,
            intent_model=ImprovementEffectIntentV1,
            derived_key=delivery_effect_key,
        )


def _delivery_receipt(payload: ImprovementEffectIntentV1, settlement_key: str) -> dict[str, object]:
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
            "settlement_key": settlement_key,
            field: document.model_dump(mode="json"),
        }
    )
    return receipt.model_dump(mode="json")
