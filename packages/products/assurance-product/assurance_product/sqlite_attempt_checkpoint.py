from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from graph_engine.attempts.checkpoint import AttemptCheckpoint
from graph_engine.attempts.keys import AttemptKey
from graph_engine.canonical import JSONValue, canonical_digest, canonical_json_bytes
from graph_engine.persistence.attempt_checkpoint import (
    ATTEMPT_CHECKPOINT_SCHEMA_VERSION,
    AttemptCheckpointIntegrityError,
    decode_attempt_checkpoint,
    encode_attempt_checkpoint,
    prepare_checkpoint_commit,
)

if TYPE_CHECKING:
    from assurance_product.sqlite_checkpointer import AssuranceSqliteBackend


class SqliteAttemptCheckpointStore:
    def __init__(self, backend: AssuranceSqliteBackend) -> None:
        self._backend = backend
        self._conn = backend._conn
        self._lock = backend.store._lock

    async def latest_generation(
        self, scope: Mapping[str, JSONValue]
    ) -> tuple[int, AttemptKey, bool, JSONValue] | None:
        async with self._lock:
            await self._assert_supported_unlocked()
            cursor = await self._conn.execute(
                "SELECT ordinal, attempt_key_digest, abandoned, scope, input_payload FROM assurance_attempt_generations "
                "WHERE scope_digest = ? ORDER BY ordinal DESC LIMIT 1",
                (canonical_digest(dict(scope)),),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        if bytes(row[3]) != canonical_json_bytes(dict(scope)):
            raise AttemptCheckpointIntegrityError("generation activation identity drifted")
        from assurance_product.worker_lifecycle import current_owner, update_owner

        owner = current_owner()
        if owner is not None and not row[2]:
            if scope.get("invocation_id") != owner.invocation:
                raise AttemptCheckpointIntegrityError("generation Invocation differs from owner")
            # Publish conservative evidence before the durable ownership update.
            update_owner(owner, lambda record: record.setdefault("attempts", []).append(str(row[1])))
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
                await self._assert_supported_unlocked()
                cursor = await self._conn.execute(
                    "SELECT ordinal, scope FROM assurance_attempt_generations WHERE scope_digest = ? "
                    "ORDER BY ordinal DESC LIMIT 1",
                    (digest,),
                )
                row = await cursor.fetchone()
                if row is not None and bytes(row[1]) != canonical_json_bytes(dict(scope)):
                    raise AttemptCheckpointIntegrityError("generation activation identity drifted")
                ordinal = 1 if row is None else int(row[0]) + 1
                if ordinal > max_attempts:
                    await self._conn.rollback()
                    return None
                key = make_key(ordinal)
                if owner is not None:
                    # A crash or rollback after this point is ambiguous, never proof of no work.
                    update_owner(owner, lambda record: record.setdefault("attempts", []).append(key.digest))
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

    async def _assert_supported_unlocked(self) -> None:
        cursor = await self._conn.execute("SELECT 1 FROM assurance_attempt_batches LIMIT 1")
        if await cursor.fetchone() is not None:
            raise AttemptCheckpointIntegrityError(
                "unsupported old Attempt journal format; stop existing runs with their original version and use a fresh isolated run"
            )

    async def load(self, attempt_key: AttemptKey) -> AttemptCheckpoint | None:
        async with self._lock:
            await self._assert_supported_unlocked()
            return await self._load_unlocked(attempt_key)

    async def _load_unlocked(self, attempt_key: AttemptKey) -> AttemptCheckpoint | None:
        cursor = await self._conn.execute(
            "SELECT attempt_key_digest, revision, schema_version, fencing_token, record_digest, payload "
            "FROM assurance_attempt_checkpoints WHERE attempt_key_digest = ?",
            (attempt_key.digest,),
        )
        row = await cursor.fetchone()
        return None if row is None else self._from_row(row)

    def _from_row(self, row: tuple[Any, ...]) -> AttemptCheckpoint:
        checkpoint = decode_attempt_checkpoint(
            bytes(row[5]), schema_version=str(row[2]), record_digest=str(row[4])
        )
        if (checkpoint.attempt_key.digest, checkpoint.revision, checkpoint.fencing_token) != (
            row[0],
            row[1],
            row[3],
        ):
            raise AttemptCheckpointIntegrityError("checkpoint row identity, revision or fence drifted")
        if checkpoint.revision < 1:
            raise AttemptCheckpointIntegrityError("stored checkpoint revision must be positive")
        return checkpoint

    async def commit(
        self, checkpoint: AttemptCheckpoint, *, expected_revision: int, fencing_token: int
    ) -> AttemptCheckpoint:
        async with self._lock:
            await self._conn.execute("BEGIN IMMEDIATE")
            try:
                await self._assert_supported_unlocked()
                await self._backend.lease.assert_current(checkpoint.invocation_id, fencing_token)
                previous = await self._load_unlocked(checkpoint.attempt_key)
                saved = prepare_checkpoint_commit(
                    checkpoint, previous, expected_revision=expected_revision, fencing_token=fencing_token
                )
                payload = encode_attempt_checkpoint(saved)
                await self._conn.execute(
                    "INSERT INTO assurance_attempt_checkpoints (attempt_key_digest, revision, schema_version, fencing_token, record_digest, payload) "
                    "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(attempt_key_digest) DO UPDATE SET "
                    "revision = excluded.revision, schema_version = excluded.schema_version, fencing_token = excluded.fencing_token, "
                    "record_digest = excluded.record_digest, payload = excluded.payload",
                    (
                        saved.attempt_key.digest,
                        saved.revision,
                        ATTEMPT_CHECKPOINT_SCHEMA_VERSION,
                        saved.fencing_token,
                        canonical_digest(saved.canonical_projection()),
                        payload,
                    ),
                )
                await self._conn.commit()
            except BaseException:
                await self._conn.rollback()
                raise
        return decode_attempt_checkpoint(payload)

    async def read_checkpoints(self) -> tuple[AttemptCheckpoint, ...]:
        async with self._lock:
            await self._assert_supported_unlocked()
            cursor = await self._conn.execute(
                "SELECT attempt_key_digest, revision, schema_version, fencing_token, record_digest, payload "
                "FROM assurance_attempt_checkpoints ORDER BY attempt_key_digest"
            )
            return tuple(self._from_row(row) for row in await cursor.fetchall())

    async def ensure_durable(self, attempt_key: AttemptKey) -> None:
        # Every commit uses the backend's FULL synchronous transaction. Loading
        # verifies the durable bytes; there is no separate event/outbox flush.
        await self.load(attempt_key)

    async def abandon_generations(self, attempt_key_digests: set[str]) -> None:
        async with self._lock:
            await self._conn.execute("BEGIN IMMEDIATE")
            try:
                await self._assert_supported_unlocked()
                await self._conn.executemany(
                    "UPDATE assurance_attempt_generations SET abandoned = 1 WHERE attempt_key_digest = ?",
                    [(digest,) for digest in attempt_key_digests],
                )
                await self._conn.commit()
            except BaseException:
                await self._conn.rollback()
                raise


__all__ = ["SqliteAttemptCheckpointStore"]
