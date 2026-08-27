"""Shared apply/reconcile loop for injected improvement stores."""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar, cast

from pydantic import BaseModel, ValidationError

from graph_engine.canonical import JSONValue
from graph_engine.frozen_json import thaw_json
from graph_engine.plugin_api import EffectApplyResult, EffectIntent, EffectReconcileResult, TaskFailure

from assurance_improvement.effects.store import ImprovementStore

_Model = TypeVar("_Model", bound=BaseModel)


class StoreCrash(RuntimeError):
    """Crash cut raised by a test store. Production stores must not raise this."""


def _payload_dict(intent: EffectIntent) -> dict[str, object]:
    thawed = thaw_json(intent.payload)
    if not isinstance(thawed, dict):
        raise ValueError("effect payload must be a mapping")
    return {str(key): value for key, value in thawed.items()}


async def apply_effect(
    *,
    store: ImprovementStore,
    intent: EffectIntent,
    idempotency_key: str,
    expected_kind: str,
    intent_model: type[_Model],
    derived_key: Callable[[_Model], str],
    build_receipt: Callable[[_Model], dict[str, object]],
    payload_key: Callable[[_Model], str] | None = None,
) -> EffectApplyResult:
    if intent.kind != expected_kind:
        return EffectApplyResult(
            status="permanent",
            failure=TaskFailure(
                kind="invalid_input",
                message=f"effect kind {intent.kind!r} is not {expected_kind}",
                retryable=False,
            ),
        )
    try:
        payload = intent_model.model_validate(_payload_dict(intent))
    except (ValidationError, ValueError) as error:
        return EffectApplyResult(
            status="permanent",
            failure=TaskFailure(kind="invalid_input", message=str(error), retryable=False),
        )
    expected = derived_key(payload)
    identity = payload_key(payload) if payload_key is not None else expected
    if idempotency_key != expected or identity != expected:
        return EffectApplyResult(
            status="permanent",
            failure=TaskFailure(
                kind="invalid_input",
                message="idempotency key does not match derived key",
                retryable=False,
            ),
        )
    existing = await store.get(idempotency_key)
    dumped = payload.model_dump(mode="json")
    if existing is not None:
        if existing.status == "applied" and existing.receipt is not None:
            if existing.payload is not None and existing.payload != dumped:
                return EffectApplyResult(
                    status="permanent",
                    failure=TaskFailure(
                        kind="invalid_output",
                        message="same key drift is corruption",
                        retryable=False,
                    ),
                )
            return EffectApplyResult.applied(cast(JSONValue, existing.receipt))
        if existing.status == "pending":
            return EffectApplyResult(
                status="transient",
                failure=TaskFailure(kind="transient", message="effect is pending", retryable=True),
            )
        if existing.status == "permanent" and existing.failure is not None:
            return EffectApplyResult(status="permanent", failure=existing.failure)
        return EffectApplyResult(
            status="transient",
            failure=TaskFailure(kind="transient", message="effect state is pending", retryable=True),
        )
    try:
        receipt = build_receipt(payload)
    except (ValidationError, ValueError) as error:
        return EffectApplyResult(
            status="permanent",
            failure=TaskFailure(kind="invalid_input", message=str(error), retryable=False),
        )
    try:
        await store.commit(idempotency_key, receipt, dumped)
    except StoreCrash:
        raise
    except RuntimeError:
        raise
    except Exception as error:
        return EffectApplyResult(
            status="transient",
            failure=TaskFailure(kind="transient", message=str(error), retryable=True),
        )
    return EffectApplyResult.applied(cast(JSONValue, receipt))


async def reconcile_effect(
    *,
    store: ImprovementStore,
    intent: EffectIntent,
    idempotency_key: str,
    expected_kind: str,
    intent_model: type[_Model],
    derived_key: Callable[[_Model], str],
    payload_key: Callable[[_Model], str] | None = None,
) -> EffectReconcileResult:
    if intent.kind != expected_kind:
        return EffectReconcileResult(
            status="permanently_failed",
            failure=TaskFailure(
                kind="invalid_input",
                message=f"effect kind {intent.kind!r} is not {expected_kind}",
                retryable=False,
            ),
        )
    try:
        payload = intent_model.model_validate(_payload_dict(intent))
    except (ValidationError, ValueError) as error:
        return EffectReconcileResult(
            status="permanently_failed",
            failure=TaskFailure(kind="invalid_input", message=str(error), retryable=False),
        )
    expected = derived_key(payload)
    identity = payload_key(payload) if payload_key is not None else expected
    if idempotency_key != expected or identity != expected:
        return EffectReconcileResult(
            status="permanently_failed",
            failure=TaskFailure(
                kind="invalid_input",
                message="idempotency key does not match derived key",
                retryable=False,
            ),
        )
    existing = await store.get(idempotency_key)
    if existing is None:
        return EffectReconcileResult(status="not_applied")
    if existing.status == "applied" and existing.receipt is not None:
        dumped = payload.model_dump(mode="json")
        if existing.payload is not None and existing.payload != dumped:
            return EffectReconcileResult(
                status="permanently_failed",
                failure=TaskFailure(
                    kind="invalid_output",
                    message="same key drift is corruption",
                    retryable=False,
                ),
            )
        return EffectReconcileResult.applied(cast(JSONValue, existing.receipt))
    if existing.status == "permanent" and existing.failure is not None:
        return EffectReconcileResult(status="permanently_failed", failure=existing.failure)
    return EffectReconcileResult(status="pending")
