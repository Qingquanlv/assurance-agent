"""Archive durable effect."""

from __future__ import annotations

from assurance_improvement.contracts.effects import (
    ArchiveApplyReceipt,
    ImprovementEffectIntentV1,
    ImprovementEffectReceiptV1,
)
from assurance_improvement.effects.common import apply_effect, reconcile_effect
from assurance_improvement.operations.keys import archive_effect_key
from graph_engine.effects.state import EffectCallContext
from graph_engine.plugin_api import EffectApplyResult, EffectIntent, EffectReconcileResult

ARCHIVE_KIND = "assurance.improvement.effect.archive.v1"
ARCHIVE_INTENT_SCHEMA = "assurance.improvement.schema.improvement-effect-intent.v1"
ARCHIVE_RECEIPT_SCHEMA = "assurance.improvement.schema.improvement-effect-receipt.v1"


class ImprovementArchiveEffect:
    async def apply(self, intent: EffectIntent, context: EffectCallContext) -> EffectApplyResult:
        return await apply_effect(
            context=context,
            intent=intent,
            expected_kind=ARCHIVE_KIND,
            intent_model=ImprovementEffectIntentV1,
            derived_key=archive_effect_key,
            build_receipt=_archive_receipt,
        )

    async def reconcile(self, intent: EffectIntent, context: EffectCallContext) -> EffectReconcileResult:
        return await reconcile_effect(
            context=context,
            intent=intent,
            expected_kind=ARCHIVE_KIND,
            intent_model=ImprovementEffectIntentV1,
            derived_key=archive_effect_key,
        )


def _archive_receipt(payload: ImprovementEffectIntentV1, settlement_key: str) -> dict[str, object]:
    receipt = ImprovementEffectReceiptV1.model_validate(
        {
            "schema_version": "1",
            "kind": "archive",
            "improvement_id": payload.improvement_id,
            "settlement_key": settlement_key,
            "archive": ArchiveApplyReceipt(
                invocation_id=payload.invocation_id or payload.improvement_id,
                archive_digest=payload.archive_digest or "0" * 64,
                summary_path=payload.artifact_path,
            ).model_dump(mode="json"),
        }
    )
    return receipt.model_dump(mode="json")
