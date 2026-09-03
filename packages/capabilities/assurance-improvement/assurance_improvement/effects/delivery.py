"""Improvement delivery durable effect, including explicit rollback."""

from __future__ import annotations

from pydantic import ValidationError

from assurance_improvement.contracts.effects import ImprovementEffectIntentV1, ImprovementEffectReceiptV1
from assurance_improvement.effects.common import apply_effect, reconcile_effect
from assurance_improvement.effects.store import ImprovementStore
from assurance_improvement.operations.keys import delivery_effect_key
from graph_engine.frozen_json import thaw_json
from graph_engine.plugin_api import EffectApplyResult, EffectIntent, EffectReconcileResult

DELIVERY_KIND = "assurance.improvement.effect.delivery.v1"
DELIVERY_INTENT_SCHEMA = "assurance.improvement.schema.improvement-effect-intent.v1"
DELIVERY_RECEIPT_SCHEMA = "assurance.improvement.schema.improvement-effect-receipt.v1"
_KERNEL_KEY_CHARS = frozenset("0123456789abcdef")


def _kernel_settlement_key(key: str) -> bool:
    return len(key) == 64 and set(key) <= _KERNEL_KEY_CHARS


def _aligned_delivery_key(intent: EffectIntent, idempotency_key: str) -> str:
    try:
        thawed = thaw_json(intent.payload)
        if not isinstance(thawed, dict):
            return idempotency_key
        formula = delivery_effect_key(ImprovementEffectIntentV1.model_validate(thawed))
    except (ValidationError, TypeError, ValueError):
        return idempotency_key
    if idempotency_key == formula or _kernel_settlement_key(idempotency_key):
        return formula
    return idempotency_key


class ImprovementDeliveryEffect:
    def __init__(self, store: ImprovementStore) -> None:
        self._store = store

    async def apply(self, intent: EffectIntent, idempotency_key: str) -> EffectApplyResult:
        return await apply_effect(
            store=self._store,
            intent=intent,
            idempotency_key=_aligned_delivery_key(intent, idempotency_key),
            expected_kind=DELIVERY_KIND,
            intent_model=ImprovementEffectIntentV1,
            derived_key=delivery_effect_key,
            build_receipt=_delivery_receipt,
        )

    async def reconcile(self, intent: EffectIntent, idempotency_key: str) -> EffectReconcileResult:
        return await reconcile_effect(
            store=self._store,
            intent=intent,
            idempotency_key=_aligned_delivery_key(intent, idempotency_key),
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
