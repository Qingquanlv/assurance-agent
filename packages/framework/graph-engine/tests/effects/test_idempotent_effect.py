from __future__ import annotations

import pytest
from pydantic import BaseModel

from graph_engine.effects.idempotent import apply_idempotent_effect, reconcile_idempotent_effect
from graph_engine.effects.state import (
    EffectStateIntegrityError,
    EffectStateObservation,
    MemoryEffectState,
    bind_effect_call,
)
from graph_engine.persistence.runner_lease import StaleFencingToken
from graph_engine.plugin_api import EffectIntent

_KIND = "example.effect.mark.v1"


class _Mark(BaseModel):
    operation_id: str
    note: str = "n"


class _Scripted:
    def __init__(
        self,
        *,
        observation: EffectStateObservation | None = None,
        observe_error: Exception | None = None,
        commit_error: Exception | None = None,
    ) -> None:
        self.settlement_key = "settle-1"
        self.observation = observation or EffectStateObservation(status="absent")
        self.observe_error = observe_error
        self.commit_error = commit_error
        self.commits = 0

    async def observe(self, *, business_key: str, intent_digest: str) -> EffectStateObservation:
        del business_key, intent_digest
        if self.observe_error is not None:
            raise self.observe_error
        return self.observation

    async def commit(
        self, *, business_key: str, intent_digest: str, payload: object, receipt: object
    ) -> EffectStateObservation:
        del business_key, intent_digest, payload
        self.commits += 1
        if self.commit_error is not None:
            raise self.commit_error
        return EffectStateObservation(status="committed", receipt=receipt)  # type: ignore[arg-type]


def _intent(payload: object, *, kind: str = _KIND) -> EffectIntent:
    return EffectIntent(kind=kind, payload=payload)  # type: ignore[arg-type]


def _context():
    return bind_effect_call(
        state=MemoryEffectState(),
        effect_kind=_KIND,
        settlement_key="settle-1",
        fencing_token=1,
    )


def _receipt(payload: _Mark, settlement_key: str) -> dict[str, object]:
    if payload.note == "bad-receipt":
        raise ValueError("receipt rejected")
    return {"operation_id": payload.operation_id, "settlement_key": settlement_key}


async def test_apply_idempotent_effect_commits_and_replays_the_receipt() -> None:
    context = _context()
    intent = _intent({"operation_id": "op-1", "note": "n"})
    calls: list[str] = []

    def build(payload: _Mark, settlement_key: str) -> dict[str, object]:
        calls.append(settlement_key)
        return _receipt(payload, settlement_key)

    first = await apply_idempotent_effect(
        context=context,
        intent=intent,
        expected_kind=_KIND,
        intent_model=_Mark,
        derived_key=lambda payload: payload.operation_id,
        build_receipt=build,
    )
    second = await apply_idempotent_effect(
        context=context,
        intent=intent,
        expected_kind=_KIND,
        intent_model=_Mark,
        derived_key=lambda payload: payload.operation_id,
        build_receipt=build,
    )

    assert first.status == "applied"
    assert first.receipt == {"operation_id": "op-1", "settlement_key": "settle-1"}
    assert second == first
    assert calls == ["settle-1"]


@pytest.mark.parametrize(
    ("payload", "kind", "message"),
    [
        ({"operation_id": "op-1"}, "example.effect.other.v1", "is not"),
        ("nope", _KIND, "mapping"),
        ({}, _KIND, "operation_id"),
        ({"operation_id": "op-1", "note": "other"}, _KIND, "idempotency key"),
    ],
)
async def test_apply_idempotent_effect_rejects_permanent_input_failures(
    payload: object, kind: str, message: str
) -> None:
    result = await apply_idempotent_effect(
        context=_context(),
        intent=_intent(payload, kind=kind),
        expected_kind=_KIND,
        intent_model=_Mark,
        derived_key=lambda item: item.operation_id,
        payload_key=lambda item: item.note,
        build_receipt=_receipt,
    )

    assert result.status == "permanent"
    assert result.failure is not None
    assert result.failure.kind == "invalid_input"
    assert result.failure.retryable is False
    assert message in result.failure.message


async def test_apply_idempotent_effect_rejects_a_receipt_builder_failure() -> None:
    result = await apply_idempotent_effect(
        context=_context(),
        intent=_intent({"operation_id": "op-1", "note": "bad-receipt"}),
        expected_kind=_KIND,
        intent_model=_Mark,
        derived_key=lambda item: item.operation_id,
        build_receipt=_receipt,
    )

    assert result.status == "permanent"
    assert result.failure is not None
    assert result.failure.kind == "invalid_input"
    assert result.failure.retryable is False
    assert "receipt rejected" in result.failure.message


async def test_apply_idempotent_effect_maps_integrity_and_commit_errors() -> None:
    observe = _Scripted(observe_error=EffectStateIntegrityError("drifted"))
    observed = await apply_idempotent_effect(
        context=observe,  # type: ignore[arg-type]
        intent=_intent({"operation_id": "op-1"}),
        expected_kind=_KIND,
        intent_model=_Mark,
        derived_key=lambda item: item.operation_id,
        build_receipt=_receipt,
    )
    commit = _Scripted(commit_error=EffectStateIntegrityError("conflict"))
    committed = await apply_idempotent_effect(
        context=commit,  # type: ignore[arg-type]
        intent=_intent({"operation_id": "op-1"}),
        expected_kind=_KIND,
        intent_model=_Mark,
        derived_key=lambda item: item.operation_id,
        build_receipt=_receipt,
    )
    stale = _Scripted(commit_error=StaleFencingToken("stale"))
    with pytest.raises(StaleFencingToken):
        await apply_idempotent_effect(
            context=stale,  # type: ignore[arg-type]
            intent=_intent({"operation_id": "op-1"}),
            expected_kind=_KIND,
            intent_model=_Mark,
            derived_key=lambda item: item.operation_id,
            build_receipt=_receipt,
        )
    runtime = _Scripted(commit_error=RuntimeError("cut"))
    with pytest.raises(RuntimeError, match="cut"):
        await apply_idempotent_effect(
            context=runtime,  # type: ignore[arg-type]
            intent=_intent({"operation_id": "op-1"}),
            expected_kind=_KIND,
            intent_model=_Mark,
            derived_key=lambda item: item.operation_id,
            build_receipt=_receipt,
        )
    transient = _Scripted(commit_error=OSError("down"))
    failed = await apply_idempotent_effect(
        context=transient,  # type: ignore[arg-type]
        intent=_intent({"operation_id": "op-1"}),
        expected_kind=_KIND,
        intent_model=_Mark,
        derived_key=lambda item: item.operation_id,
        build_receipt=_receipt,
    )

    assert observed.failure is not None and observed.failure.kind == "invalid_output"
    assert observed.failure.retryable is False
    assert committed.failure is not None and committed.failure.kind == "invalid_output"
    assert committed.failure.retryable is False
    assert failed.status == "transient"
    assert failed.failure is not None and failed.failure.retryable is True


async def test_reconcile_idempotent_effect_reports_each_state() -> None:
    context = _context()
    intent = _intent({"operation_id": "op-1"})
    absent = await reconcile_idempotent_effect(
        context=context,
        intent=intent,
        expected_kind=_KIND,
        intent_model=_Mark,
        derived_key=lambda item: item.operation_id,
    )
    applied = await apply_idempotent_effect(
        context=context,
        intent=intent,
        expected_kind=_KIND,
        intent_model=_Mark,
        derived_key=lambda item: item.operation_id,
        build_receipt=_receipt,
    )
    again = await reconcile_idempotent_effect(
        context=context,
        intent=intent,
        expected_kind=_KIND,
        intent_model=_Mark,
        derived_key=lambda item: item.operation_id,
    )
    pending = await reconcile_idempotent_effect(
        context=_Scripted(observation=EffectStateObservation(status="committed", receipt=None)),  # type: ignore[arg-type]
        intent=intent,
        expected_kind=_KIND,
        intent_model=_Mark,
        derived_key=lambda item: item.operation_id,
    )

    assert absent.status == "not_applied"
    assert again.status == "applied"
    assert again.receipt == applied.receipt
    assert pending.status == "pending"


@pytest.mark.parametrize(
    ("payload", "kind", "message"),
    [
        ({"operation_id": "op-1"}, "example.effect.other.v1", "is not"),
        ({}, _KIND, "operation_id"),
        ({"operation_id": "op-1", "note": "other"}, _KIND, "idempotency key"),
    ],
)
async def test_reconcile_idempotent_effect_rejects_permanent_input_failures(
    payload: object, kind: str, message: str
) -> None:
    result = await reconcile_idempotent_effect(
        context=_context(),
        intent=_intent(payload, kind=kind),
        expected_kind=_KIND,
        intent_model=_Mark,
        derived_key=lambda item: item.operation_id,
        payload_key=lambda item: item.note,
    )

    assert result.status == "permanently_failed"
    assert result.failure is not None
    assert result.failure.kind == "invalid_input"
    assert result.failure.retryable is False
    assert message in result.failure.message


async def test_reconcile_idempotent_effect_maps_integrity_failure() -> None:
    result = await reconcile_idempotent_effect(
        context=_Scripted(observe_error=EffectStateIntegrityError("drifted")),  # type: ignore[arg-type]
        intent=_intent({"operation_id": "op-1"}),
        expected_kind=_KIND,
        intent_model=_Mark,
        derived_key=lambda item: item.operation_id,
    )

    assert result.status == "permanently_failed"
    assert result.failure is not None
    assert result.failure.kind == "invalid_output"
    assert result.failure.retryable is False
