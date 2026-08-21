from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

from graph_engine.composition.models import EffectEntry, EffectRegistry, SchemaRegistry
from graph_engine.errors import GraphEngineError
from graph_engine.plugin_api import (
    EffectApplyResult,
    EffectIntent,
    EffectReconcileResult,
    TaskFailure,
)
from graph_engine.runtime.events import (
    EffectApplyStarted,
    EffectReceiptRecorded,
    RuntimeEvent,
    TaskAttemptFailed,
    TaskAttemptSucceeded,
)
from graph_engine.runtime.frozen_json import thaw_json
from graph_engine.runtime.json_schema import validate_json_schema
from graph_engine.runtime.ledger import Ledger, append_validated_batch
from graph_engine.runtime.models import AttemptRecord, EffectRecord, InvocationProjection

_Result = TypeVar("_Result")
_SETTLEABLE = {"committed", "applying"}
_EXHAUSTED = TaskFailure(
    kind="external_effect",
    message="effect apply attempts exhausted",
    retryable=False,
)
_INVALID_RECEIPT = TaskFailure(
    kind="invalid_output",
    message="effect receipt does not match the registered schema",
    retryable=False,
)


class EffectStateError(GraphEngineError):
    """Raised when a projected effect cannot be settled."""


class EffectPublicationIndeterminate(GraphEngineError):
    """Raised when an effect handler fails without a typed, durable result."""


@dataclass(frozen=True, slots=True)
class EffectSettlement:
    progressed: bool
    pending: bool = False

    def __post_init__(self) -> None:
        if self.pending and self.progressed:
            raise ValueError("pending settlement cannot have progressed")


def needs_settlement(projection: InvocationProjection) -> bool:
    if any(item.status in _SETTLEABLE for item in projection.effects):
        return True
    return any(
        item.attempts and item.attempts[-1].status == "effect_pending" for item in projection.activations
    )


class EffectExecutor:
    def __init__(
        self,
        effects: EffectRegistry,
        schemas: SchemaRegistry,
        ledger: Ledger,
        *,
        transition_guard: Callable[[], None] | None = None,
    ) -> None:
        self._effects = effects
        self._schemas = schemas
        self._ledger = ledger
        self._transition_guard = transition_guard

    async def settle_next(self, projection: InvocationProjection) -> EffectSettlement:
        effect = _next_effect(projection)
        if effect is None:
            return self._publish_ready_task_success(projection)
        try:
            registration = self._effects.require(effect.kind)
        except KeyError as error:
            raise EffectStateError(f"unknown effect kind: {effect.kind}") from error
        if effect.status == "committed":
            apply_attempt = self._append_apply_started(effect)
            return await self._apply(registration, effect, apply_attempt)
        if effect.status == "applying":
            return await self._reconcile(registration, effect)
        raise EffectStateError(f"effect {effect.effect_id} is not settleable")

    def _publish_ready_task_success(self, projection: InvocationProjection) -> EffectSettlement:
        ready: list[tuple[str, AttemptRecord]] = []
        for activation in projection.activations:
            if not activation.attempts:
                continue
            attempt = activation.attempts[-1]
            if attempt.status != "effect_pending" or attempt.prepared_commit is None:
                continue
            if not _all_receipts_present(projection, activation.activation_id, attempt):
                continue
            ready.append((activation.activation_id, attempt))
        if not ready:
            return EffectSettlement(progressed=False)
        ready.sort(
            key=lambda item: (
                item[1].prepared_commit.task_id if item[1].prepared_commit is not None else item[0],
                item[1].attempt,
                item[0],
            )
        )
        activation_id, attempt = ready[0]
        self._append(
            TaskAttemptSucceeded(
                activation_id=activation_id,
                attempt=attempt.attempt,
                output=attempt.output,
            )
        )
        return EffectSettlement(progressed=True)

    async def _apply(
        self,
        registration: EffectEntry,
        effect: EffectRecord,
        apply_attempt: int,
    ) -> EffectSettlement:
        result = await self._invoke(
            registration.handler.apply(_intent(effect), effect.idempotency_key),
            registration.policy.timeout_seconds,
            "apply",
        )
        if not isinstance(result, EffectApplyResult):
            raise EffectPublicationIndeterminate("effect apply returned an untyped result")
        return await self._handle_apply_result(registration, effect, apply_attempt, result)

    async def _reconcile(self, registration: EffectEntry, effect: EffectRecord) -> EffectSettlement:
        result = await self._invoke(
            registration.handler.reconcile(_intent(effect), effect.idempotency_key),
            registration.policy.timeout_seconds,
            "reconcile",
        )
        if not isinstance(result, EffectReconcileResult):
            raise EffectPublicationIndeterminate("effect reconcile returned an untyped result")
        if result.status == "applied":
            return self._publish_receipt(registration, effect, effect.apply_attempts, result.receipt)
        if result.status == "permanently_failed":
            assert result.failure is not None
            return self._publish_failure(effect, result.failure)
        if result.status == "pending":
            return EffectSettlement(progressed=False, pending=True)
        if result.status == "not_applied":
            if effect.apply_attempts >= registration.policy.max_attempts:
                return self._publish_failure(effect, _EXHAUSTED)
            return await self._apply(registration, effect, effect.apply_attempts)
        raise EffectStateError(f"unknown reconcile status: {result.status}")

    async def _handle_apply_result(
        self,
        registration: EffectEntry,
        effect: EffectRecord,
        apply_attempt: int,
        result: EffectApplyResult,
    ) -> EffectSettlement:
        if result.status == "applied":
            return self._publish_receipt(registration, effect, apply_attempt, result.receipt)
        if result.status == "permanent":
            assert result.failure is not None
            return self._publish_failure(effect, result.failure)
        if result.status == "transient":
            if apply_attempt >= registration.policy.max_attempts:
                return self._publish_failure(effect, _EXHAUSTED)
            if registration.policy.backoff_seconds > 0:
                await asyncio.sleep(registration.policy.backoff_seconds)
            next_attempt = apply_attempt + 1
            if next_attempt <= registration.policy.max_attempts:
                self._append(
                    EffectApplyStarted(
                        effect_id=effect.effect_id,
                        apply_attempt=next_attempt,
                    )
                )
            return EffectSettlement(progressed=True)
        raise EffectStateError(f"unknown apply status: {result.status}")

    def _publish_receipt(
        self,
        registration: EffectEntry,
        effect: EffectRecord,
        apply_attempt: int,
        receipt: object,
    ) -> EffectSettlement:
        try:
            self._validate_receipt(registration, receipt)
        except ValueError:
            return self._publish_failure(effect, _INVALID_RECEIPT)
        self._append(
            EffectReceiptRecorded(
                effect_id=effect.effect_id,
                apply_attempt=apply_attempt,
                receipt=thaw_json(receipt),
            )
        )
        return EffectSettlement(progressed=True)

    def _validate_receipt(self, registration: EffectEntry, receipt: object) -> None:
        schema = self._schemas.entries.get(registration.receipt_schema_id)
        if schema is None:
            raise ValueError(f"effect receipt schema is not registered: {registration.receipt_schema_id}")
        validate_json_schema(thaw_json(receipt), schema.content)

    def _publish_failure(self, effect: EffectRecord, failure: TaskFailure) -> EffectSettlement:
        self._append(
            TaskAttemptFailed(
                activation_id=effect.activation_id,
                attempt=effect.task_attempt,
                failure=failure,
            )
        )
        return EffectSettlement(progressed=True)

    def _append_apply_started(self, effect: EffectRecord) -> int:
        apply_attempt = effect.apply_attempts + 1
        self._append(EffectApplyStarted(effect_id=effect.effect_id, apply_attempt=apply_attempt))
        return apply_attempt

    def _append(self, event: RuntimeEvent) -> None:
        existing = self._ledger.read_all()
        expected_next_seq = existing[-1].seq + 1 if existing else 1
        self._guard_transition()
        append_validated_batch(self._ledger, (event,), expected_next_seq=expected_next_seq)
        self._guard_transition()

    def _guard_transition(self) -> None:
        if self._transition_guard is not None:
            self._transition_guard()

    async def _invoke(
        self,
        awaitable: Awaitable[_Result],
        timeout_seconds: float,
        action: str,
    ) -> _Result:
        try:
            return await asyncio.wait_for(awaitable, timeout=timeout_seconds)
        except EffectPublicationIndeterminate:
            raise
        except TimeoutError as error:
            raise EffectPublicationIndeterminate(f"effect {action} timed out") from error
        except Exception as error:
            raise EffectPublicationIndeterminate(f"effect {action} failed without a typed result") from error


def _next_effect(projection: InvocationProjection) -> EffectRecord | None:
    pending = [item for item in projection.effects if item.status in _SETTLEABLE]
    pending.sort(key=lambda item: (item.task_id, item.task_attempt, item.index, item.effect_id))
    for effect in pending:
        predecessors = [
            item
            for item in projection.effects
            if item.task_id == effect.task_id
            and item.task_attempt == effect.task_attempt
            and item.index < effect.index
        ]
        if all(item.status == "applied" for item in predecessors):
            return effect
    return None


def _all_receipts_present(
    projection: InvocationProjection,
    activation_id: str,
    attempt: AttemptRecord,
) -> bool:
    prepared = attempt.prepared_commit
    if prepared is None:
        return False
    records = tuple(
        item
        for item in projection.effects
        if item.activation_id == activation_id and item.task_attempt == attempt.attempt
    )
    if len(records) != len(prepared.effect_ids):
        return False
    return all(item.status == "applied" for item in records)


def _intent(effect: EffectRecord) -> EffectIntent:
    return EffectIntent(kind=effect.kind, payload=thaw_json(effect.payload))
