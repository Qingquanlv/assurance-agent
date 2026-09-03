from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence

from graph_engine.attempts.context import AttemptExecutionContext
from graph_engine.attempts.events import (
    AttemptEffectState,
    AttemptEvent,
    AttemptSnapshot,
    EffectApplied,
    EffectIntentRecorded,
    EffectReceiptRecorded,
)
from graph_engine.attempts.keys import AttemptKey
from graph_engine.attempts.resolutions import (
    AttemptResolution,
    CommittedEffectFailure,
    IndeterminateTaskResult,
    PendingTaskResult,
    ReceiptRef,
    SystemReference,
)
from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.composition.models import EffectEntry, EffectRegistry, SchemaRegistry
from graph_engine.effects.contracts import EXPECTED_EFFECT_KINDS, effect_idempotency_key
from graph_engine.effects.recovery import intent_from_state, next_effect_action
from graph_engine.frozen_json import thaw_json
from graph_engine.json_schema import validate_json_schema
from graph_engine.persistence.attempt_journal import AttemptJournalPort
from graph_engine.plugin_api import (
    EffectApplyResult,
    EffectIntent,
    EffectReconcileResult,
    PromotionReceipt,
)

_EXHAUSTED_REASON = "effect apply attempts exhausted"
_INVALID_RECEIPT_REASON = "effect receipt does not match the registered schema"


class AttemptEffectSettler:
    def __init__(
        self,
        effects: EffectRegistry,
        schemas: SchemaRegistry,
    ) -> None:
        self._effects = effects
        self._schemas = schemas

    async def settle(
        self,
        *,
        attempt_key: AttemptKey,
        snapshot: AttemptSnapshot,
        journal: AttemptJournalPort,
        context: AttemptExecutionContext,
        intents: Sequence[EffectIntent],
        promotion: PromotionReceipt,
        cut: Callable[[str], None],
    ) -> tuple[AttemptResolution | None, AttemptSnapshot]:
        cut("after_promotion_before_effect")
        snapshot = await self._record_intents(attempt_key, snapshot, journal, context, tuple(intents))
        promotion_receipt = ReceiptRef(
            receipt_id=promotion.identity_digest,
            receipt_digest=promotion.receipt_digest,
        )
        for state in snapshot.effects:
            action = next_effect_action(state)
            if action == "done":
                continue
            registration = self._effects.require(state.kind)
            intent = intent_from_state(state)
            key = effect_idempotency_key(attempt_key, state.ordinal)
            if action == "apply":
                resolution, snapshot = await self._apply(
                    attempt_key,
                    snapshot,
                    journal,
                    context,
                    registration,
                    intent,
                    state,
                    key,
                    promotion_receipt,
                )
            else:
                resolution, snapshot = await self._reconcile(
                    attempt_key,
                    snapshot,
                    journal,
                    context,
                    registration,
                    intent,
                    state,
                    key,
                    promotion_receipt,
                )
            if resolution is not None:
                return resolution, snapshot
        return None, snapshot

    async def _record_intents(
        self,
        attempt_key: AttemptKey,
        snapshot: AttemptSnapshot,
        journal: AttemptJournalPort,
        context: AttemptExecutionContext,
        intents: tuple[EffectIntent, ...],
    ) -> AttemptSnapshot:
        recorded = {item.ordinal: item for item in snapshot.effects}
        events: list[AttemptEvent] = []
        for ordinal, intent in enumerate(intents, start=1):
            if intent.kind not in self._effects.entries or intent.kind not in EXPECTED_EFFECT_KINDS:
                raise KeyError(f"unknown effect kind: {intent.kind}")
            self._validate_intent(self._effects.require(intent.kind), intent)
            digest = _intent_digest(intent)
            existing = recorded.get(ordinal)
            if existing is not None:
                if existing.kind != intent.kind or existing.intent_digest != digest:
                    raise ValueError("declared effect intent drifted")
                continue
            events.append(
                EffectIntentRecorded(
                    effect_ordinal=ordinal,
                    effect_kind=intent.kind,
                    intent_digest=digest,
                    payload=thaw_json(intent.payload),
                )
            )
        if not events:
            return snapshot
        return await journal.append(
            attempt_key,
            tuple(events),
            expected_revision=snapshot.revision,
            fencing_token=context.fencing_token,
        )

    async def _apply(
        self,
        attempt_key: AttemptKey,
        snapshot: AttemptSnapshot,
        journal: AttemptJournalPort,
        context: AttemptExecutionContext,
        registration: EffectEntry,
        intent: EffectIntent,
        state: AttemptEffectState,
        key: str,
        promotion_receipt: ReceiptRef,
    ) -> tuple[AttemptResolution | None, AttemptSnapshot]:
        result = await self._invoke(
            registration.handler.apply(intent, key),
            registration.policy.timeout_seconds,
            "apply",
        )
        snapshot = await self._record_apply(attempt_key, snapshot, journal, context, state)
        if not isinstance(result, EffectApplyResult):
            return (
                IndeterminateTaskResult(
                    reconciliation=SystemReference(
                        reference_id=f"effect-reconcile:{attempt_key.digest}:{state.ordinal}"
                    )
                ),
                snapshot,
            )
        if result.status == "applied":
            return await self._publish_receipt(
                attempt_key,
                snapshot,
                journal,
                context,
                registration,
                state,
                result.receipt,
                promotion_receipt,
            )
        if result.status == "permanent":
            assert result.failure is not None
            return (
                CommittedEffectFailure(
                    writes_promoted=True,
                    promotion_receipt=promotion_receipt,
                    reason=result.failure.message,
                ),
                snapshot,
            )
        if result.status == "transient":
            return (
                IndeterminateTaskResult(
                    reconciliation=SystemReference(
                        reference_id=f"effect-reconcile:{attempt_key.digest}:{state.ordinal}"
                    )
                ),
                snapshot,
            )
        return (
            IndeterminateTaskResult(
                reconciliation=SystemReference(
                    reference_id=f"effect-reconcile:{attempt_key.digest}:{state.ordinal}"
                )
            ),
            snapshot,
        )

    async def _reconcile(
        self,
        attempt_key: AttemptKey,
        snapshot: AttemptSnapshot,
        journal: AttemptJournalPort,
        context: AttemptExecutionContext,
        registration: EffectEntry,
        intent: EffectIntent,
        state: AttemptEffectState,
        key: str,
        promotion_receipt: ReceiptRef,
    ) -> tuple[AttemptResolution | None, AttemptSnapshot]:
        result = await self._invoke(
            registration.handler.reconcile(intent, key),
            registration.policy.timeout_seconds,
            "reconcile",
        )
        if not isinstance(result, EffectReconcileResult):
            return (
                IndeterminateTaskResult(
                    reconciliation=SystemReference(
                        reference_id=f"effect-reconcile:{attempt_key.digest}:{state.ordinal}"
                    )
                ),
                snapshot,
            )
        if result.status == "applied":
            return await self._publish_receipt(
                attempt_key,
                snapshot,
                journal,
                context,
                registration,
                state,
                result.receipt,
                promotion_receipt,
            )
        if result.status == "permanently_failed":
            assert result.failure is not None
            return (
                CommittedEffectFailure(
                    writes_promoted=True,
                    promotion_receipt=promotion_receipt,
                    reason=result.failure.message,
                ),
                snapshot,
            )
        if result.status == "pending":
            return (
                PendingTaskResult(
                    wakeup=SystemReference(reference_id=f"effect:{attempt_key.digest}:{state.ordinal}")
                ),
                snapshot,
            )
        if result.status == "not_applied":
            if state.apply_attempts >= registration.policy.max_attempts:
                return (
                    CommittedEffectFailure(
                        writes_promoted=True,
                        promotion_receipt=promotion_receipt,
                        reason=_EXHAUSTED_REASON,
                    ),
                    snapshot,
                )
            return await self._apply(
                attempt_key,
                snapshot,
                journal,
                context,
                registration,
                intent,
                state,
                key,
                promotion_receipt,
            )
        return (
            IndeterminateTaskResult(
                reconciliation=SystemReference(
                    reference_id=f"effect-reconcile:{attempt_key.digest}:{state.ordinal}"
                )
            ),
            snapshot,
        )

    async def _publish_receipt(
        self,
        attempt_key: AttemptKey,
        snapshot: AttemptSnapshot,
        journal: AttemptJournalPort,
        context: AttemptExecutionContext,
        registration: EffectEntry,
        state: AttemptEffectState,
        receipt: object,
        promotion_receipt: ReceiptRef,
    ) -> tuple[AttemptResolution | None, AttemptSnapshot]:
        try:
            self._validate_receipt(registration, receipt)
        except ValueError:
            return (
                CommittedEffectFailure(
                    writes_promoted=True,
                    promotion_receipt=promotion_receipt,
                    reason=_INVALID_RECEIPT_REASON,
                ),
                snapshot,
            )
        payload: JSONValue = thaw_json(receipt)
        snapshot = await journal.append(
            attempt_key,
            (
                EffectReceiptRecorded(
                    effect_ordinal=state.ordinal,
                    receipt_digest=canonical_digest(payload),
                ),
            ),
            expected_revision=snapshot.revision,
            fencing_token=context.fencing_token,
        )
        return None, snapshot

    async def _record_apply(
        self,
        attempt_key: AttemptKey,
        snapshot: AttemptSnapshot,
        journal: AttemptJournalPort,
        context: AttemptExecutionContext,
        state: AttemptEffectState,
    ) -> AttemptSnapshot:
        apply_attempt = state.apply_attempts + 1
        return await journal.append(
            attempt_key,
            (
                EffectApplied(
                    effect_ordinal=state.ordinal,
                    apply_digest=canonical_digest(
                        {
                            "attempt_key": attempt_key.digest,
                            "effect_ordinal": state.ordinal,
                            "apply_attempt": apply_attempt,
                        }
                    ),
                ),
            ),
            expected_revision=snapshot.revision,
            fencing_token=context.fencing_token,
        )

    def _validate_intent(self, registration: EffectEntry, intent: EffectIntent) -> None:
        schema = self._schemas.entries.get(registration.intent_schema_id)
        if schema is None:
            raise ValueError(f"effect intent schema is not registered: {registration.intent_schema_id}")
        validate_json_schema(thaw_json(intent.payload), schema.content)

    def _validate_receipt(self, registration: EffectEntry, receipt: object) -> None:
        schema = self._schemas.entries.get(registration.receipt_schema_id)
        if schema is None:
            raise ValueError(f"effect receipt schema is not registered: {registration.receipt_schema_id}")
        validate_json_schema(thaw_json(receipt), schema.content)

    async def _invoke(
        self,
        awaitable: object,
        timeout_seconds: float,
        action: str,
    ) -> object:
        del action
        try:
            return await asyncio.wait_for(awaitable, timeout=timeout_seconds)  # type: ignore[arg-type]
        except Exception:
            return None


def _intent_digest(intent: EffectIntent) -> str:
    payload: JSONValue = thaw_json(intent.payload)
    return canonical_digest({"kind": intent.kind, "payload": payload})


__all__ = ["AttemptEffectSettler"]
