"""Heal-apply durable effect."""

from __future__ import annotations

from assurance_healing.contracts.effects import HealApplyIntentV2, HealApplyReceiptV2
from assurance_healing.contracts.wire import heal_apply_intent_digest
from assurance_healing.operations.keys import derive_heal_record_key
from graph_engine.effects.idempotent import apply_idempotent_effect, reconcile_idempotent_effect
from graph_engine.effects.state import EffectCallContext
from graph_engine.plugin_api import EffectApplyResult, EffectIntent, EffectReconcileResult

HEAL_APPLY_KIND = "assurance.healing.effect.heal-apply.v2"
HEAL_APPLY_INTENT_SCHEMA = "assurance.healing.schema.heal-apply-intent.v2"
HEAL_APPLY_RECEIPT_SCHEMA = "assurance.healing.schema.heal-apply-receipt.v2"


class HealApplyEffect:
    async def apply(self, intent: EffectIntent, context: EffectCallContext) -> EffectApplyResult:
        return await apply_idempotent_effect(
            context=context,
            intent=intent,
            expected_kind=HEAL_APPLY_KIND,
            intent_model=HealApplyIntentV2,
            derived_key=_heal_formula,
            payload_key=lambda payload: payload.record_key,
            build_receipt=_heal_receipt,
        )

    async def reconcile(self, intent: EffectIntent, context: EffectCallContext) -> EffectReconcileResult:
        return await reconcile_idempotent_effect(
            context=context,
            intent=intent,
            expected_kind=HEAL_APPLY_KIND,
            intent_model=HealApplyIntentV2,
            derived_key=_heal_formula,
            payload_key=lambda payload: payload.record_key,
        )


def _heal_formula(payload: HealApplyIntentV2) -> str:
    return derive_heal_record_key(
        owner_id=payload.owner_id,
        write_set_id=payload.write_set_id,
        candidate_digest=payload.candidate_digest,
        safety_payload_digest=payload.safety_payload_digest,
        target=payload.target,
    )


def _heal_receipt(payload: HealApplyIntentV2, settlement_key: str) -> dict[str, object]:
    dumped = payload.model_dump(mode="json")
    receipt = HealApplyReceiptV2.model_validate(
        {
            **dumped,
            "idempotency_key": payload.record_key,
            "intent_digest": heal_apply_intent_digest(dumped),
            "settlement_key": settlement_key,
        }
    )
    return receipt.model_dump(mode="json")
