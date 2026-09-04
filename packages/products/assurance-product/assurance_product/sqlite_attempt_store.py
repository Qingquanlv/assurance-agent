from __future__ import annotations

import json
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from graph_engine.attempts.events import AttemptEvent, AttemptSnapshot, fold_attempt_events
from graph_engine.attempts.keys import AttemptKey
from graph_engine.canonical import canonical_json_bytes
from graph_engine.persistence.attempt_journal import (
    ATTEMPT_JOURNAL_SCHEMA_VERSION,
    AttemptJournalIntegrityError,
    AttemptJournalRecord,
    decode_attempt_journal_record,
)
from graph_engine.persistence.runner_lease import StaleFencingToken

if TYPE_CHECKING:
    from assurance_product.sqlite_checkpointer import AssuranceSqliteBackend


def _fencing_token(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError("fencing token must be a positive integer")
    return value


def _revision(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("revision must be a non-negative integer")
    return value


class SqliteAttemptJournal:
    def __init__(self, backend: AssuranceSqliteBackend) -> None:
        self._backend = backend
        self._conn = backend._conn
        self._lock = backend.store._lock

    async def load(self, attempt_key: AttemptKey) -> AttemptSnapshot | None:
        records = await self._load_records(attempt_key.digest)
        if not records:
            return None
        return self._snapshot(attempt_key, records)

    async def append(
        self,
        attempt_key: AttemptKey,
        events: Sequence[AttemptEvent],
        *,
        expected_revision: int,
        fencing_token: int,
    ) -> AttemptSnapshot:
        record = AttemptJournalRecord.build(
            revision=expected_revision,
            attempt_key=attempt_key,
            fencing_token=fencing_token,
            events=events,
        )
        return await self.append_record(
            record, expected_revision=expected_revision, fencing_token=fencing_token
        )

    async def append_record(
        self,
        record: AttemptJournalRecord,
        *,
        expected_revision: int,
        fencing_token: int,
    ) -> AttemptSnapshot:
        if not isinstance(record, AttemptJournalRecord):
            raise TypeError("record must be an AttemptJournalRecord")
        if record.record_digest != record.canonical_digest():
            raise AttemptJournalIntegrityError("attempt journal digest drifted")
        token = _fencing_token(fencing_token)
        if record.fencing_token != token:
            raise AttemptJournalIntegrityError("fencing token drifted from the record")
        revision = _revision(expected_revision)
        if record.revision != revision:
            raise AttemptJournalIntegrityError("revision gap")
        key_digest = record.attempt_key_digest
        async with self._lock:
            await self._conn.execute("BEGIN IMMEDIATE")
            try:
                existing = await self._load_records_unlocked(key_digest)
                if existing and token < existing[-1].fencing_token:
                    raise StaleFencingToken("fencing token is stale")
                if 0 <= revision < len(existing):
                    if existing[revision] == record:
                        await self._conn.rollback()
                        return self._snapshot(AttemptKey(digest=key_digest), existing)
                    raise AttemptJournalIntegrityError("compare-and-swap conflict")
                if revision != len(existing):
                    raise AttemptJournalIntegrityError("revision gap")
                await self._insert_unlocked(record)
                existing.append(record)
                await self._conn.commit()
            except BaseException:
                await self._conn.rollback()
                raise
        return self._snapshot(AttemptKey(digest=key_digest), existing)

    async def ensure_durable(self, attempt_key: AttemptKey) -> None:
        loaded = await self.load(attempt_key)
        if loaded is None:
            return
        async with self._lock:
            await self._conn.execute("BEGIN IMMEDIATE")
            try:
                await self._conn.execute(
                    "INSERT INTO assurance_attempt_durable "
                    "(attempt_key_digest, durable_revision) VALUES (?, ?) "
                    "ON CONFLICT(attempt_key_digest) DO UPDATE SET "
                    "durable_revision = excluded.durable_revision",
                    (attempt_key.digest, loaded.revision),
                )
                await self._conn.commit()
            except BaseException:
                await self._conn.rollback()
                raise

    async def read_records(self) -> tuple[AttemptJournalRecord, ...]:
        async with self._lock:
            cursor = await self._conn.execute(
                "SELECT revision, schema_version, fencing_token, record_digest, payload "
                "FROM assurance_attempt_batches ORDER BY attempt_key_digest, revision"
            )
            rows = await cursor.fetchall()
        return tuple(self._record_from_row(row) for row in rows)

    async def _load_records(self, attempt_key_digest: str) -> list[AttemptJournalRecord]:
        async with self._lock:
            return await self._load_records_unlocked(attempt_key_digest)

    async def _load_records_unlocked(self, attempt_key_digest: str) -> list[AttemptJournalRecord]:
        cursor = await self._conn.execute(
            "SELECT revision, schema_version, fencing_token, record_digest, payload "
            "FROM assurance_attempt_batches WHERE attempt_key_digest = ? ORDER BY revision",
            (attempt_key_digest,),
        )
        rows = await cursor.fetchall()
        records: list[AttemptJournalRecord] = []
        for index, row in enumerate(rows):
            if int(row[0]) != index:
                raise AttemptJournalIntegrityError("revision gap")
            records.append(self._record_from_row(row))
        return records

    async def _insert_unlocked(self, record: AttemptJournalRecord) -> None:
        await self._conn.execute(
            "INSERT INTO assurance_attempt_batches ("
            "attempt_key_digest, revision, schema_version, fencing_token, record_digest, payload"
            ") VALUES (?, ?, ?, ?, ?, ?)",
            (
                record.attempt_key_digest,
                record.revision,
                ATTEMPT_JOURNAL_SCHEMA_VERSION,
                record.fencing_token,
                record.record_digest,
                canonical_json_bytes(record.canonical_projection()),
            ),
        )

    def _record_from_row(self, row: tuple[Any, ...]) -> AttemptJournalRecord:
        return decode_attempt_journal_record(
            json.loads(bytes(row[4] or b"").decode("utf-8")),
            schema_version=str(row[1]),
            record_digest=str(row[3]),
        )

    def _snapshot(self, attempt_key: AttemptKey, records: Sequence[AttemptJournalRecord]) -> AttemptSnapshot:
        events = tuple(event for record in records for event in record.events)
        return fold_attempt_events(
            attempt_key,
            events,
            revision=len(records),
            fencing_token=records[-1].fencing_token,
        )


__all__ = ["SqliteAttemptJournal"]
