"""Archive durable effect."""

from __future__ import annotations

from assurance_improvement.contracts.effects import (
    ArchiveApplyReceipt,
    ImprovementEffectIntentV1,
    ImprovementEffectReceiptV1,
)
from assurance_improvement.effects.common import apply_effect, reconcile_effect
from assurance_improvement.effects.store import ImprovementStore
from assurance_improvement.operations.keys import archive_effect_key
from graph_engine.plugin_api import EffectApplyResult, EffectIntent, EffectReconcileResult

ARCHIVE_KIND = "assurance.improvement.effect.archive.v1"
ARCHIVE_INTENT_SCHEMA = "assurance.improvement.schema.improvement-effect-intent.v1"
ARCHIVE_RECEIPT_SCHEMA = "assurance.improvement.schema.improvement-effect-receipt.v1"


class ImprovementArchiveEffect:
    def __init__(self, store: ImprovementStore) -> None:
        self._store = store

    async def apply(self, intent: EffectIntent, idempotency_key: str) -> EffectApplyResult:
        return await apply_effect(
            store=self._store,
            intent=intent,
            idempotency_key=idempotency_key,
            expected_kind=ARCHIVE_KIND,
            intent_model=ImprovementEffectIntentV1,
            derived_key=archive_effect_key,
            build_receipt=_archive_receipt,
        )

    async def reconcile(self, intent: EffectIntent, idempotency_key: str) -> EffectReconcileResult:
        return await reconcile_effect(
            store=self._store,
            intent=intent,
            idempotency_key=idempotency_key,
            expected_kind=ARCHIVE_KIND,
            intent_model=ImprovementEffectIntentV1,
            derived_key=archive_effect_key,
        )


def _archive_receipt(payload: ImprovementEffectIntentV1) -> dict[str, object]:
    receipt = ImprovementEffectReceiptV1.model_validate(
        {
            "schema_version": "1",
            "kind": "archive",
            "improvement_id": payload.improvement_id,
            "archive": ArchiveApplyReceipt(
                invocation_id=payload.invocation_id or payload.improvement_id,
                archive_digest=payload.archive_digest or "0" * 64,
                summary_path=payload.artifact_path,
            ).model_dump(mode="json"),
        }
    )
    return receipt.model_dump(mode="json")
