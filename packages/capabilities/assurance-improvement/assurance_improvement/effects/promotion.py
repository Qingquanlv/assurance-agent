"""Test-promotion durable effect."""

from __future__ import annotations

from assurance_improvement.contracts.effects import ImprovementEffectIntentV1, ImprovementEffectReceiptV1
from assurance_improvement.operations.keys import promotion_effect_key
from graph_engine.effects.idempotent import apply_idempotent_effect, reconcile_idempotent_effect
from graph_engine.effects.state import EffectCallContext
from graph_engine.plugin_api import EffectApplyResult, EffectIntent, EffectReconcileResult

PROMOTION_KIND = "assurance.improvement.effect.promotion.v1"
PROMOTION_INTENT_SCHEMA = "assurance.improvement.schema.improvement-effect-intent.v1"
PROMOTION_RECEIPT_SCHEMA = "assurance.improvement.schema.improvement-effect-receipt.v1"


class ImprovementPromotionEffect:
    async def apply(self, intent: EffectIntent, context: EffectCallContext) -> EffectApplyResult:
        return await apply_idempotent_effect(
            context=context,
            intent=intent,
            expected_kind=PROMOTION_KIND,
            intent_model=ImprovementEffectIntentV1,
            derived_key=promotion_effect_key,
            build_receipt=_promotion_receipt,
        )

    async def reconcile(self, intent: EffectIntent, context: EffectCallContext) -> EffectReconcileResult:
        return await reconcile_idempotent_effect(
            context=context,
            intent=intent,
            expected_kind=PROMOTION_KIND,
            intent_model=ImprovementEffectIntentV1,
            derived_key=promotion_effect_key,
        )


def _promotion_receipt(payload: ImprovementEffectIntentV1, settlement_key: str) -> dict[str, object]:
    if payload.promotion is None:
        raise ValueError("promotion intent is missing the promotion receipt")
    receipt = ImprovementEffectReceiptV1.model_validate(
        {
            "schema_version": "1",
            "kind": "test_promotion",
            "improvement_id": payload.improvement_id,
            "settlement_key": settlement_key,
            "promotion": payload.promotion.model_dump(mode="json"),
        }
    )
    return receipt.model_dump(mode="json")
