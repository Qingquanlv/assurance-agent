from __future__ import annotations

from types import SimpleNamespace

import pytest

from graph_engine.composition.models import EffectRegistry
from graph_engine.effects.state import MemoryEffectState, bind_effect_call
from graph_engine.persistence.runner_lease import StaleFencingToken

_DELIVERY = "assurance.improvement.effect.delivery.v1"
_SETTLEMENT = "a" * 64
_DIGEST = "b" * 64
_BUSINESS = "delivery:IMP-1"
_PAYLOAD = {"kind": "memory_apply"}
_RECEIPT = {"idempotency_key": _BUSINESS, "settlement_key": _SETTLEMENT}


@pytest.fixture
def memory_effect_state() -> MemoryEffectState:
    return MemoryEffectState()


@pytest.fixture
def composition() -> SimpleNamespace:
    return SimpleNamespace(registries=SimpleNamespace(effects=EffectRegistry(entries={})))


async def test_context_prevents_settlement_key_substitution(memory_effect_state) -> None:
    context = bind_effect_call(
        state=memory_effect_state,
        effect_kind="assurance.improvement.effect.delivery.v1",
        settlement_key="a" * 64,
        fencing_token=7,
    )
    await context.commit(
        business_key="delivery:IMP-1",
        intent_digest="b" * 64,
        payload={"kind": "memory_apply"},
        receipt={"idempotency_key": "delivery:IMP-1", "settlement_key": "a" * 64},
    )
    record = await memory_effect_state.observe(
        effect_kind="assurance.improvement.effect.delivery.v1",
        settlement_key="a" * 64,
        business_key="delivery:IMP-1",
        intent_digest="b" * 64,
        fencing_token=7,
    )
    assert record.status == "committed"


def test_frozen_composition_exposes_one_effect_registry(composition) -> None:
    assert isinstance(composition.registries.effects, EffectRegistry)
    assert not hasattr(composition, "effect_factory_registry")
    assert not hasattr(composition, "runtime_effect_registry")


async def test_exact_receipt_reuse_is_idempotent(memory_effect_state) -> None:
    context = bind_effect_call(
        state=memory_effect_state,
        effect_kind=_DELIVERY,
        settlement_key=_SETTLEMENT,
        fencing_token=3,
    )
    first = await context.commit(
        business_key=_BUSINESS,
        intent_digest=_DIGEST,
        payload=_PAYLOAD,
        receipt=_RECEIPT,
    )
    second = await context.commit(
        business_key=_BUSINESS,
        intent_digest=_DIGEST,
        payload=_PAYLOAD,
        receipt=_RECEIPT,
    )
    assert first.status == "committed"
    assert second.status == "committed"
    assert first.receipt == _RECEIPT
    assert second.receipt == _RECEIPT


async def test_intent_drift_conflicts(memory_effect_state) -> None:
    context = bind_effect_call(
        state=memory_effect_state,
        effect_kind=_DELIVERY,
        settlement_key=_SETTLEMENT,
        fencing_token=3,
    )
    await context.commit(
        business_key=_BUSINESS,
        intent_digest=_DIGEST,
        payload=_PAYLOAD,
        receipt=_RECEIPT,
    )
    with pytest.raises(Exception, match="intent"):
        await context.commit(
            business_key=_BUSINESS,
            intent_digest="c" * 64,
            payload={"kind": "memory_rollback"},
            receipt=_RECEIPT,
        )


async def test_settlement_and_business_keys_are_unique(memory_effect_state) -> None:
    first = bind_effect_call(
        state=memory_effect_state,
        effect_kind=_DELIVERY,
        settlement_key=_SETTLEMENT,
        fencing_token=3,
    )
    await first.commit(
        business_key=_BUSINESS,
        intent_digest=_DIGEST,
        payload=_PAYLOAD,
        receipt=_RECEIPT,
    )
    other_settlement = bind_effect_call(
        state=memory_effect_state,
        effect_kind=_DELIVERY,
        settlement_key="c" * 64,
        fencing_token=3,
    )
    with pytest.raises(Exception):
        await other_settlement.commit(
            business_key=_BUSINESS,
            intent_digest=_DIGEST,
            payload=_PAYLOAD,
            receipt={"idempotency_key": _BUSINESS, "settlement_key": "c" * 64},
        )
    same_settlement = bind_effect_call(
        state=memory_effect_state,
        effect_kind=_DELIVERY,
        settlement_key=_SETTLEMENT,
        fencing_token=3,
    )
    with pytest.raises(Exception):
        await same_settlement.commit(
            business_key="delivery:IMP-2",
            intent_digest=_DIGEST,
            payload=_PAYLOAD,
            receipt={"idempotency_key": "delivery:IMP-2", "settlement_key": _SETTLEMENT},
        )


async def test_stale_fence_is_rejected(memory_effect_state) -> None:
    context = bind_effect_call(
        state=memory_effect_state,
        effect_kind=_DELIVERY,
        settlement_key=_SETTLEMENT,
        fencing_token=7,
    )
    await context.commit(
        business_key=_BUSINESS,
        intent_digest=_DIGEST,
        payload=_PAYLOAD,
        receipt=_RECEIPT,
    )
    stale = bind_effect_call(
        state=memory_effect_state,
        effect_kind=_DELIVERY,
        settlement_key=_SETTLEMENT,
        fencing_token=6,
    )
    with pytest.raises(StaleFencingToken):
        await stale.observe(business_key=_BUSINESS, intent_digest=_DIGEST)
    with pytest.raises(StaleFencingToken):
        await stale.commit(
            business_key=_BUSINESS,
            intent_digest=_DIGEST,
            payload=_PAYLOAD,
            receipt=_RECEIPT,
        )


async def test_observe_absent_before_commit(memory_effect_state) -> None:
    context = bind_effect_call(
        state=memory_effect_state,
        effect_kind=_DELIVERY,
        settlement_key=_SETTLEMENT,
        fencing_token=3,
    )
    record = await context.observe(business_key=_BUSINESS, intent_digest=_DIGEST)
    assert record.status == "absent"
    assert record.receipt is None
