from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from assurance_product.change_workspace import ChangeWorkspace
from assurance_product.sqlite_checkpointer import open_sqlite_checkpointer
from assurance_product.sqlite_effect_state import SQLiteEffectState
from graph_engine.canonical import JSONValue
from graph_engine.effects.state import bind_effect_call
from graph_engine.persistence.runner_lease import StaleFencingToken

_KIND = "assurance.improvement.effect.delivery.v1"
_SETTLEMENT = "a" * 64
_DIGEST = "b" * 64
_BUSINESS = "delivery:IMP-1"
_PAYLOAD: JSONValue = {"kind": "memory_apply"}
_RECEIPT: JSONValue = {"idempotency_key": _BUSINESS, "settlement_key": _SETTLEMENT}
_ROLLBACK_PAYLOAD: JSONValue = {"kind": "memory_rollback"}
_OTHER_RECEIPT: JSONValue = {"idempotency_key": _BUSINESS, "settlement_key": "c" * 64}


@pytest.fixture
def workspace(tmp_path: Path) -> ChangeWorkspace:
    project = (tmp_path / "project").resolve()
    (project / "qa" / "changes" / "CH-1").mkdir(parents=True)
    return ChangeWorkspace.prepare(project, "CH-1")


def test_effect_state_survives_backend_restart(workspace) -> None:
    asyncio.run(_effect_state_survives_backend_restart(workspace))


async def _effect_state_survives_backend_restart(workspace) -> None:
    async with open_sqlite_checkpointer(workspace) as backend:
        state = SQLiteEffectState(backend)
        context = bind_effect_call(
            state=state,
            effect_kind=_KIND,
            settlement_key=_SETTLEMENT,
            fencing_token=7,
        )
        await context.commit(
            business_key=_BUSINESS,
            intent_digest=_DIGEST,
            payload=_PAYLOAD,
            receipt=_RECEIPT,
        )
    async with open_sqlite_checkpointer(workspace) as reopened:
        record = await SQLiteEffectState(reopened).observe(
            effect_kind=_KIND,
            settlement_key=_SETTLEMENT,
            business_key=_BUSINESS,
            intent_digest=_DIGEST,
            fencing_token=7,
        )
        assert record.status == "committed"
        assert record.receipt == _RECEIPT


def test_sqlite_exact_reuse_and_conflicts(workspace) -> None:
    asyncio.run(_sqlite_exact_reuse_and_conflicts(workspace))


async def _sqlite_exact_reuse_and_conflicts(workspace) -> None:
    async with open_sqlite_checkpointer(workspace) as backend:
        state = SQLiteEffectState(backend)
        context = bind_effect_call(
            state=state,
            effect_kind=_KIND,
            settlement_key=_SETTLEMENT,
            fencing_token=4,
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
        assert first.receipt == second.receipt
        with pytest.raises(Exception, match="intent"):
            await context.commit(
                business_key=_BUSINESS,
                intent_digest="c" * 64,
                payload=_ROLLBACK_PAYLOAD,
                receipt=_RECEIPT,
            )
        other = bind_effect_call(
            state=state,
            effect_kind=_KIND,
            settlement_key="c" * 64,
            fencing_token=4,
        )
        with pytest.raises(Exception):
            await other.commit(
                business_key=_BUSINESS,
                intent_digest=_DIGEST,
                payload=_PAYLOAD,
                receipt=_OTHER_RECEIPT,
            )


def test_sqlite_stale_fence_is_rejected(workspace) -> None:
    asyncio.run(_sqlite_stale_fence_is_rejected(workspace))


async def _sqlite_stale_fence_is_rejected(workspace) -> None:
    async with open_sqlite_checkpointer(workspace) as backend:
        state = SQLiteEffectState(backend)
        context = bind_effect_call(
            state=state,
            effect_kind=_KIND,
            settlement_key=_SETTLEMENT,
            fencing_token=5,
        )
        await context.commit(
            business_key=_BUSINESS,
            intent_digest=_DIGEST,
            payload=_PAYLOAD,
            receipt=_RECEIPT,
        )
        stale = bind_effect_call(
            state=state,
            effect_kind=_KIND,
            settlement_key=_SETTLEMENT,
            fencing_token=4,
        )
        with pytest.raises(StaleFencingToken):
            await stale.observe(business_key=_BUSINESS, intent_digest=_DIGEST)
