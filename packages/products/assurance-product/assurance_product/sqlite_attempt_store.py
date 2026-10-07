from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from graph_engine.attempts.events import AttemptEvent, AttemptSnapshot, fold_attempt_events
from graph_engine.attempts.keys import AttemptKey
from graph_engine.canonical import JSONValue, canonical_digest, canonical_json_bytes
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

    async def latest_generation(
        self, scope: Mapping[str, JSONValue]
    ) -> tuple[int, AttemptKey, bool, JSONValue] | None:
        async with self._lock:
            cursor = await self._conn.execute(
                "SELECT ordinal, attempt_key_digest, abandoned, scope, input_payload FROM assurance_attempt_generations "
                "WHERE scope_digest = ? ORDER BY ordinal DESC LIMIT 1",
                (canonical_digest(dict(scope)),),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        if bytes(row[3]) != canonical_json_bytes(dict(scope)):
            raise AttemptJournalIntegrityError("generation activation identity drifted")
        from assurance_product.worker_lifecycle import current_owner, update_owner

        owner = current_owner()
        if owner is not None and not row[2]:
            if scope.get("invocation_id") != owner.invocation:
                raise AttemptJournalIntegrityError("generation Invocation differs from owner")
            # Publish conservative evidence before the durable ownership update.
            update_owner(owner, lambda record: record["attempts"].append(str(row[1])))
            async with self._lock:
                await self._conn.execute(
                    "UPDATE assurance_attempt_generations SET owner_nonce = ? WHERE attempt_key_digest = ?",
                    (owner.nonce, str(row[1])),
                )
                await self._conn.commit()
        return int(row[0]), AttemptKey(digest=str(row[1])), bool(row[2]), json.loads(bytes(row[4]))

    async def register_generation(
        self,
        scope: Mapping[str, JSONValue],
        make_key: Callable[[int], AttemptKey],
        *,
        max_attempts: int,
        validated_input: JSONValue = None,
    ) -> tuple[int, AttemptKey] | None:
        from assurance_product.worker_lifecycle import current_owner, assert_generation_ready, update_owner

        owner = current_owner()
        if owner is not None:
            assert_generation_ready(owner)
        digest = canonical_digest(dict(scope))
        async with self._lock:
            await self._conn.execute("BEGIN IMMEDIATE")
            try:
                cursor = await self._conn.execute(
                    "SELECT ordinal, scope FROM assurance_attempt_generations WHERE scope_digest = ? "
                    "ORDER BY ordinal DESC LIMIT 1",
                    (digest,),
                )
                row = await cursor.fetchone()
                if row is not None and bytes(row[1]) != canonical_json_bytes(dict(scope)):
                    raise AttemptJournalIntegrityError("generation activation identity drifted")
                ordinal = 1 if row is None else int(row[0]) + 1
                if row is None:
                    cursor = await self._conn.execute(
                        "SELECT payload FROM assurance_attempt_batches WHERE revision = 0 AND attempt_key_digest NOT IN (SELECT attempt_key_digest FROM assurance_attempt_generations)"
                    )
                    for (payload,) in await cursor.fetchall():
                        events = json.loads(bytes(payload))["events"]
                        if any(
                            event.get("kind") == "attempt_opened"
                            and event.get("invocation_id") == scope.get("invocation_id")
                            and event.get("semantic_node_id") == scope.get("semantic_node_id")
                            for event in events
                        ):
                            raise AttemptJournalIntegrityError(
                                "legacy unfinished node has no authenticated generation budget; use a fresh isolated run"
                            )
                if ordinal > max_attempts:
                    await self._conn.rollback()
                    return None
                key = make_key(ordinal)
                if owner is not None:
                    # A crash or rollback after this point is ambiguous, never proof of no work.
                    update_owner(owner, lambda record: record["attempts"].append(key.digest))
                await self._conn.execute(
                    "INSERT INTO assurance_attempt_generations (scope_digest, ordinal, scope, attempt_key_digest, owner_nonce, input_payload) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        digest,
                        ordinal,
                        canonical_json_bytes(dict(scope)),
                        key.digest,
                        None if owner is None else owner.nonce,
                        canonical_json_bytes(validated_input),
                    ),
                )
                await self._conn.commit()
                return ordinal, key
            except BaseException:
                await self._conn.rollback()
                raise

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
