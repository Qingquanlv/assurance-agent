from __future__ import annotations

import asyncio
from collections.abc import Callable

from graph_engine.attempts.context import AttemptExecutionContext
from graph_engine.attempts.events import (
    AttemptEffectState,
    AttemptSnapshot,
    EffectApplied,
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
from graph_engine.effects.contracts import effect_idempotency_key
from graph_engine.effects.recovery import intent_from_state, next_effect_action
from graph_engine.effects.state import (
    EffectCallContext,
    EffectStatePort,
    bind_effect_call,
)
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
        state: EffectStatePort,
    ) -> None:
        self._effects = effects
        self._schemas = schemas
        self._state = state

    async def settle(
        self,
        *,
        attempt_key: AttemptKey,
        snapshot: AttemptSnapshot,
        journal: AttemptJournalPort,
        context: AttemptExecutionContext,
        promotion: PromotionReceipt,
        cut: Callable[[str], None],
    ) -> tuple[AttemptResolution | None, AttemptSnapshot]:
        cut("after_promotion_before_effect")
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
            call = bind_effect_call(
                state=self._state,
                effect_kind=state.kind,
                settlement_key=effect_idempotency_key(attempt_key, state.ordinal),
                fencing_token=context.fencing_token,
            )
            if action == "apply":
                resolution, snapshot = await self._apply(
                    attempt_key,
                    snapshot,
                    journal,
                    context,
                    registration,
                    intent,
                    state,
                    call,
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
                    call,
                    promotion_receipt,
                )
            if resolution is not None:
                return resolution, snapshot
        return None, snapshot

    async def _apply(
        self,
        attempt_key: AttemptKey,
        snapshot: AttemptSnapshot,
        journal: AttemptJournalPort,
        context: AttemptExecutionContext,
        registration: EffectEntry,
        intent: EffectIntent,
        state: AttemptEffectState,
        call: EffectCallContext,
        promotion_receipt: ReceiptRef,
    ) -> tuple[AttemptResolution | None, AttemptSnapshot]:
        result = await self._invoke(
            registration.handler.apply(intent, call),
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
        call: EffectCallContext,
        promotion_receipt: ReceiptRef,
    ) -> tuple[AttemptResolution | None, AttemptSnapshot]:
        result = await self._invoke(
            registration.handler.reconcile(intent, call),
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
                call,
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


__all__ = ["AttemptEffectSettler"]
