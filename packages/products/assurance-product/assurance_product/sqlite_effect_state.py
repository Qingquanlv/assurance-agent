from __future__ import annotations

import json
from typing import TYPE_CHECKING

from graph_engine.canonical import JSONValue, canonical_json_bytes
from graph_engine.effects.state import (
    EffectStateIntegrityError,
    EffectStateObservation,
    EffectStateRecord,
    assert_effect_reuse,
    effect_observation,
    require_fencing_token,
)
from graph_engine.frozen_json import thaw_json
from graph_engine.persistence.runner_lease import StaleFencingToken

if TYPE_CHECKING:
    from assurance_product.sqlite_checkpointer import AssuranceSqliteBackend

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS assurance_effect_state (
    effect_kind TEXT NOT NULL,
    settlement_key TEXT NOT NULL,
    business_key TEXT NOT NULL,
    intent_digest TEXT NOT NULL,
    fencing_token INTEGER NOT NULL,
    payload BLOB NOT NULL,
    receipt BLOB NOT NULL,
    PRIMARY KEY (effect_kind, settlement_key),
    UNIQUE (effect_kind, business_key)
)
"""


class SQLiteEffectState:
    def __init__(self, backend: AssuranceSqliteBackend) -> None:
        self._backend = backend
        self._conn = backend._conn
        self._lock = backend.store._lock

    async def commit(
        self,
        *,
        effect_kind: str,
        settlement_key: str,
        business_key: str,
        intent_digest: str,
        payload: JSONValue,
        receipt: JSONValue,
        fencing_token: int,
    ) -> EffectStateObservation:
        token = require_fencing_token(fencing_token)
        async with self._lock:
            await self._conn.execute("BEGIN IMMEDIATE")
            try:
                await self._conn.execute(_CREATE_TABLE)
                existing = await self._load_unlocked(effect_kind, settlement_key)
                owner = await self._load_business_unlocked(effect_kind, business_key)
                if existing is not None:
                    if token < existing.fencing_token:
                        raise StaleFencingToken("fencing token is stale")
                    assert_effect_reuse(
                        existing,
                        business_key=business_key,
                        intent_digest=intent_digest,
                        receipt=receipt,
                    )
                    await self._conn.rollback()
                    return effect_observation(existing)
                if owner is not None:
                    raise EffectStateIntegrityError("effect business key conflict")
                record = EffectStateRecord(
                    effect_kind=effect_kind,
                    settlement_key=settlement_key,
                    business_key=business_key,
                    intent_digest=intent_digest,
                    payload=thaw_json(payload),
                    receipt=thaw_json(receipt),
                    fencing_token=token,
                )
                await self._conn.execute(
                    "INSERT INTO assurance_effect_state ("
                    "effect_kind, settlement_key, business_key, intent_digest, "
                    "fencing_token, payload, receipt"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        record.effect_kind,
                        record.settlement_key,
                        record.business_key,
                        record.intent_digest,
                        record.fencing_token,
                        canonical_json_bytes(record.payload),
                        canonical_json_bytes(record.receipt),
                    ),
                )
                await self._conn.commit()
            except BaseException:
                await self._conn.rollback()
                raise
        return effect_observation(record)

    async def observe(
        self,
        *,
        effect_kind: str,
        settlement_key: str,
        business_key: str,
        intent_digest: str,
        fencing_token: int,
    ) -> EffectStateObservation:
        token = require_fencing_token(fencing_token)
        async with self._lock:
            await self._conn.execute("BEGIN IMMEDIATE")
            try:
                await self._conn.execute(_CREATE_TABLE)
                existing = await self._load_unlocked(effect_kind, settlement_key)
                owner = await self._load_business_unlocked(effect_kind, business_key)
                if existing is None:
                    if owner is not None:
                        raise EffectStateIntegrityError("effect business key conflict")
                    await self._conn.rollback()
                    return EffectStateObservation(status="absent")
                if token < existing.fencing_token:
                    raise StaleFencingToken("fencing token is stale")
                assert_effect_reuse(
                    existing,
                    business_key=business_key,
                    intent_digest=intent_digest,
                    receipt=None,
                )
                await self._conn.rollback()
            except BaseException:
                await self._conn.rollback()
                raise
        return effect_observation(existing)

    async def _load_unlocked(self, effect_kind: str, settlement_key: str) -> EffectStateRecord | None:
        cursor = await self._conn.execute(
            "SELECT effect_kind, settlement_key, business_key, intent_digest, "
            "fencing_token, payload, receipt FROM assurance_effect_state "
            "WHERE effect_kind = ? AND settlement_key = ?",
            (effect_kind, settlement_key),
        )
        row = await cursor.fetchone()
        return None if row is None else _record_from_row(row)

    async def _load_business_unlocked(self, effect_kind: str, business_key: str) -> EffectStateRecord | None:
        cursor = await self._conn.execute(
            "SELECT effect_kind, settlement_key, business_key, intent_digest, "
            "fencing_token, payload, receipt FROM assurance_effect_state "
            "WHERE effect_kind = ? AND business_key = ?",
            (effect_kind, business_key),
        )
        row = await cursor.fetchone()
        return None if row is None else _record_from_row(row)


def _record_from_row(row: tuple[object, ...]) -> EffectStateRecord:
    payload = bytes(row[5]) if isinstance(row[5], (bytes, bytearray)) else b""
    receipt = bytes(row[6]) if isinstance(row[6], (bytes, bytearray)) else b""
    return EffectStateRecord(
        effect_kind=str(row[0]),
        settlement_key=str(row[1]),
        business_key=str(row[2]),
        intent_digest=str(row[3]),
        fencing_token=int(str(row[4])),
        payload=json.loads(payload.decode("utf-8")),
        receipt=json.loads(receipt.decode("utf-8")),
    )


__all__ = ["SQLiteEffectState"]
