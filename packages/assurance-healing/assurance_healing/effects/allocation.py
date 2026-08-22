"""Healing allocation durable effect."""

from __future__ import annotations

from assurance_healing.contracts.effects import HealingAllocationIntentV2, HealingAllocationReceiptV2
from assurance_healing.effects.common import apply_effect, reconcile_effect
from assurance_healing.effects.store import HealingStore
from assurance_healing.operations.keys import derive_allocation_ids
from graph_engine.plugin_api import EffectApplyResult, EffectIntent, EffectReconcileResult

ALLOCATION_KIND = "assurance.healing.effect.allocation.v2"
ALLOCATION_INTENT_SCHEMA = "assurance.healing.schema.allocation-intent.v2"
ALLOCATION_RECEIPT_SCHEMA = "assurance.healing.schema.allocation-receipt.v2"


class HealingAllocationEffect:
    def __init__(self, store: HealingStore) -> None:
        self._store = store

    async def apply(self, intent: EffectIntent, idempotency_key: str) -> EffectApplyResult:
        return await apply_effect(
            store=self._store,
            intent=intent,
            idempotency_key=idempotency_key,
            expected_kind=ALLOCATION_KIND,
            intent_model=HealingAllocationIntentV2,
            derived_key=_allocation_formula,
            payload_key=lambda payload: payload.operation_id,
            build_receipt=_allocation_receipt,
        )

    async def reconcile(self, intent: EffectIntent, idempotency_key: str) -> EffectReconcileResult:
        return await reconcile_effect(
            store=self._store,
            intent=intent,
            idempotency_key=idempotency_key,
            expected_kind=ALLOCATION_KIND,
            intent_model=HealingAllocationIntentV2,
            derived_key=_allocation_formula,
            payload_key=lambda payload: payload.operation_id,
        )


def _allocation_formula(payload: HealingAllocationIntentV2) -> str:
    return str(
        derive_allocation_ids(
            change_id=payload.change_id,
            source_batch_id=payload.source_batch_id,
            entry_batch_id=payload.entry_batch_id,
            candidate_digest=payload.candidate_digest,
            attempt_number=payload.attempt_number,
        )["operation_id"]
    )


def _allocation_receipt(payload: HealingAllocationIntentV2) -> dict[str, object]:
    receipt = HealingAllocationReceiptV2.model_validate(
        {**payload.model_dump(mode="json"), "idempotency_key": payload.operation_id}
    )
    return receipt.model_dump(mode="json")
