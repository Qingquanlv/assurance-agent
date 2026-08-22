"""Test-promotion durable effect."""

from __future__ import annotations

from assurance_improvement.contracts.effects import ImprovementEffectIntentV1, ImprovementEffectReceiptV1
from assurance_improvement.effects.common import apply_effect, reconcile_effect
from assurance_improvement.effects.store import ImprovementStore
from assurance_improvement.operations.keys import promotion_effect_key
from graph_engine.plugin_api import EffectApplyResult, EffectIntent, EffectReconcileResult

PROMOTION_KIND = "assurance.improvement.effect.promotion.v1"
PROMOTION_INTENT_SCHEMA = "assurance.improvement.schema.improvement-effect-intent.v1"
PROMOTION_RECEIPT_SCHEMA = "assurance.improvement.schema.improvement-effect-receipt.v1"


class ImprovementPromotionEffect:
    def __init__(self, store: ImprovementStore) -> None:
        self._store = store

    async def apply(self, intent: EffectIntent, idempotency_key: str) -> EffectApplyResult:
        return await apply_effect(
            store=self._store,
            intent=intent,
            idempotency_key=idempotency_key,
            expected_kind=PROMOTION_KIND,
            intent_model=ImprovementEffectIntentV1,
            derived_key=promotion_effect_key,
            build_receipt=_promotion_receipt,
        )

    async def reconcile(self, intent: EffectIntent, idempotency_key: str) -> EffectReconcileResult:
        return await reconcile_effect(
            store=self._store,
            intent=intent,
            idempotency_key=idempotency_key,
            expected_kind=PROMOTION_KIND,
            intent_model=ImprovementEffectIntentV1,
            derived_key=promotion_effect_key,
        )


def _promotion_receipt(payload: ImprovementEffectIntentV1) -> dict[str, object]:
    if payload.promotion is None:
        raise ValueError("promotion intent is missing the promotion receipt")
    receipt = ImprovementEffectReceiptV1.model_validate(
        {
            "schema_version": "1",
            "kind": "test_promotion",
            "improvement_id": payload.improvement_id,
            "promotion": payload.promotion.model_dump(mode="json"),
        }
    )
    return receipt.model_dump(mode="json")
