"""Shared apply/reconcile loop for context-driven improvement effects."""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar, cast

from pydantic import BaseModel, ValidationError

from graph_engine.canonical import JSONValue
from graph_engine.effects.state import EffectCallContext, EffectStateIntegrityError, effect_intent_digest
from graph_engine.frozen_json import thaw_json
from graph_engine.plugin_api import EffectApplyResult, EffectIntent, EffectReconcileResult, TaskFailure

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
    context: EffectCallContext,
    intent: EffectIntent,
    expected_kind: str,
    intent_model: type[_Model],
    derived_key: Callable[[_Model], str],
    build_receipt: Callable[[_Model, str], dict[str, object]],
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
    if identity != expected:
        return EffectApplyResult(
            status="permanent",
            failure=TaskFailure(
                kind="invalid_input",
                message="idempotency key does not match derived key",
                retryable=False,
            ),
        )
    dumped = payload.model_dump(mode="json")
    digest = effect_intent_digest(intent.kind, dumped)
    existing = await context.observe(business_key=expected, intent_digest=digest)
    if existing.status == "committed" and existing.receipt is not None:
        return EffectApplyResult.applied(cast(JSONValue, existing.receipt))
    try:
        receipt = build_receipt(payload, context.settlement_key)
    except (ValidationError, ValueError) as error:
        return EffectApplyResult(
            status="permanent",
            failure=TaskFailure(kind="invalid_input", message=str(error), retryable=False),
        )
    try:
        committed = await context.commit(
            business_key=expected,
            intent_digest=digest,
            payload=cast(JSONValue, dumped),
            receipt=cast(JSONValue, receipt),
        )
    except EffectStateIntegrityError as error:
        return EffectApplyResult(
            status="permanent",
            failure=TaskFailure(kind="invalid_output", message=str(error), retryable=False),
        )
    except StoreCrash:
        raise
    except RuntimeError:
        raise
    except Exception as error:
        return EffectApplyResult(
            status="transient",
            failure=TaskFailure(kind="transient", message=str(error), retryable=True),
        )
    return EffectApplyResult.applied(cast(JSONValue, committed.receipt or receipt))


async def reconcile_effect(
    *,
    context: EffectCallContext,
    intent: EffectIntent,
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
    if identity != expected:
        return EffectReconcileResult(
            status="permanently_failed",
            failure=TaskFailure(
                kind="invalid_input",
                message="idempotency key does not match derived key",
                retryable=False,
            ),
        )
    dumped = payload.model_dump(mode="json")
    digest = effect_intent_digest(intent.kind, dumped)
    try:
        existing = await context.observe(business_key=expected, intent_digest=digest)
    except EffectStateIntegrityError as error:
        return EffectReconcileResult(
            status="permanently_failed",
            failure=TaskFailure(kind="invalid_output", message=str(error), retryable=False),
        )
    if existing.status == "absent":
        return EffectReconcileResult(status="not_applied")
    if existing.status == "committed" and existing.receipt is not None:
        return EffectReconcileResult.applied(cast(JSONValue, existing.receipt))
    return EffectReconcileResult(status="pending")
