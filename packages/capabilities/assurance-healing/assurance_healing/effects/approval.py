"""Proposal-approved durable effect."""

from __future__ import annotations

from assurance_healing.contracts.effects import ProposalApprovedIntentV1, ProposalApprovedReceiptV1
from assurance_healing.operations.keys import derive_approval_id
from graph_engine.effects.idempotent import apply_idempotent_effect, reconcile_idempotent_effect
from graph_engine.effects.state import EffectCallContext
from graph_engine.plugin_api import EffectApplyResult, EffectIntent, EffectReconcileResult

APPROVAL_KIND = "assurance.healing.effect.proposal-approved.v1"
APPROVAL_INTENT_SCHEMA = "assurance.healing.schema.proposal-approved-intent.v1"
APPROVAL_RECEIPT_SCHEMA = "assurance.healing.schema.proposal-approved-receipt.v1"


class ProposalApprovedEffect:
    async def apply(self, intent: EffectIntent, context: EffectCallContext) -> EffectApplyResult:
        return await apply_idempotent_effect(
            context=context,
            intent=intent,
            expected_kind=APPROVAL_KIND,
            intent_model=ProposalApprovedIntentV1,
            derived_key=_approval_formula,
            payload_key=lambda payload: payload.approval_id,
            build_receipt=_approval_receipt,
        )

    async def reconcile(self, intent: EffectIntent, context: EffectCallContext) -> EffectReconcileResult:
        return await reconcile_idempotent_effect(
            context=context,
            intent=intent,
            expected_kind=APPROVAL_KIND,
            intent_model=ProposalApprovedIntentV1,
            derived_key=_approval_formula,
            payload_key=lambda payload: payload.approval_id,
        )


def _approval_formula(payload: ProposalApprovedIntentV1) -> str:
    return derive_approval_id(
        owner_id=payload.owner_id,
        candidate_digest=payload.candidate_digest,
        baseline_digest=payload.baseline_digest,
        policy_digest=payload.policy_digest,
        proposal_digest=payload.proposal_digest,
    )


def _approval_receipt(payload: ProposalApprovedIntentV1, settlement_key: str) -> dict[str, object]:
    receipt = ProposalApprovedReceiptV1.model_validate(
        {
            **payload.model_dump(mode="json"),
            "idempotency_key": payload.approval_id,
            "settlement_key": settlement_key,
        }
    )
    return receipt.model_dump(mode="json")
