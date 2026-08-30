"""Proposal-approved durable effect."""

from __future__ import annotations

from assurance_healing.contracts.effects import ProposalApprovedIntentV1, ProposalApprovedReceiptV1
from assurance_healing.effects.common import apply_effect, reconcile_effect
from assurance_healing.effects.store import HealingStore
from assurance_healing.operations.keys import derive_approval_id
from graph_engine.plugin_api import EffectApplyResult, EffectIntent, EffectReconcileResult

APPROVAL_KIND = "assurance.healing.effect.proposal-approved.v1"
APPROVAL_INTENT_SCHEMA = "assurance.healing.schema.proposal-approved-intent.v1"
APPROVAL_RECEIPT_SCHEMA = "assurance.healing.schema.proposal-approved-receipt.v1"


class ProposalApprovedEffect:
    def __init__(self, store: HealingStore) -> None:
        self._store = store

    async def apply(self, intent: EffectIntent, idempotency_key: str) -> EffectApplyResult:
        return await apply_effect(
            store=self._store,
            intent=intent,
            idempotency_key=idempotency_key,
            expected_kind=APPROVAL_KIND,
            intent_model=ProposalApprovedIntentV1,
            derived_key=_approval_formula,
            payload_key=lambda payload: payload.approval_id,
            build_receipt=_approval_receipt,
        )

    async def reconcile(self, intent: EffectIntent, idempotency_key: str) -> EffectReconcileResult:
        return await reconcile_effect(
            store=self._store,
            intent=intent,
            idempotency_key=idempotency_key,
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


def _approval_receipt(payload: ProposalApprovedIntentV1) -> dict[str, object]:
    receipt = ProposalApprovedReceiptV1.model_validate(
        {**payload.model_dump(mode="json"), "idempotency_key": payload.approval_id}
    )
    return receipt.model_dump(mode="json")
