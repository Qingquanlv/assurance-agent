"""Heal-apply durable effect."""

from __future__ import annotations

from assurance_healing.contracts.effects import HealApplyIntentV2, HealApplyReceiptV2
from assurance_healing.contracts.wire import heal_apply_intent_digest
from assurance_healing.effects.common import apply_effect, reconcile_effect
from assurance_healing.effects.store import HealingStore
from assurance_healing.operations.keys import derive_heal_record_key
from graph_engine.plugin_api import EffectApplyResult, EffectIntent, EffectReconcileResult

HEAL_APPLY_KIND = "assurance.healing.effect.heal-apply.v2"
HEAL_APPLY_INTENT_SCHEMA = "assurance.healing.schema.heal-apply-intent.v2"
HEAL_APPLY_RECEIPT_SCHEMA = "assurance.healing.schema.heal-apply-receipt.v2"


class HealApplyEffect:
    def __init__(self, store: HealingStore) -> None:
        self._store = store

    async def apply(self, intent: EffectIntent, idempotency_key: str) -> EffectApplyResult:
        return await apply_effect(
            store=self._store,
            intent=intent,
            idempotency_key=idempotency_key,
            expected_kind=HEAL_APPLY_KIND,
            intent_model=HealApplyIntentV2,
            derived_key=_heal_formula,
            payload_key=lambda payload: payload.record_key,
            build_receipt=_heal_receipt,
        )

    async def reconcile(self, intent: EffectIntent, idempotency_key: str) -> EffectReconcileResult:
        return await reconcile_effect(
            store=self._store,
            intent=intent,
            idempotency_key=idempotency_key,
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


def _heal_receipt(payload: HealApplyIntentV2) -> dict[str, object]:
    dumped = payload.model_dump(mode="json")
    receipt = HealApplyReceiptV2.model_validate(
        {
            **dumped,
            "idempotency_key": payload.record_key,
            "intent_digest": heal_apply_intent_digest(dumped),
        }
    )
    return receipt.model_dump(mode="json")
