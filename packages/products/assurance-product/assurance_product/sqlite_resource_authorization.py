from __future__ import annotations

import json
from typing import TYPE_CHECKING

from graph_engine.canonical import canonical_json_bytes
from graph_engine.persistence.resource_authorization import (
    RESOURCE_AUTHORIZATION_SCHEMA_VERSION,
    ResourceAuthorizationIntegrityError,
    ResourceAuthorizationRecord,
    active_authorization_grant,
    assert_authorization_transition,
    decode_resource_authorization_record,
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


class SqliteResourceAuthorizationStore:
    def __init__(self, backend: AssuranceSqliteBackend) -> None:
        self._backend = backend
        self._conn = backend._conn
        self._lock = backend.store._lock

    async def append(
        self,
        record: ResourceAuthorizationRecord,
        *,
        expected_revision: int,
        fencing_token: int,
    ) -> None:
        if not isinstance(record, ResourceAuthorizationRecord):
            raise TypeError("record must be a ResourceAuthorizationRecord")
        if record.record_digest != record.canonical_digest():
            raise ResourceAuthorizationIntegrityError("resource authorization digest drifted")
        token = _fencing_token(fencing_token)
        if record.fencing_token != token:
            raise ResourceAuthorizationIntegrityError("fencing token drifted from the record")
        revision = _revision(expected_revision)
        async with self._lock:
            await self._conn.execute("BEGIN IMMEDIATE")
            try:
                existing = await self._load_records_unlocked()
                if 0 <= revision < len(existing):
                    if existing[revision] == record:
                        await self._conn.rollback()
                        return
                    raise ResourceAuthorizationIntegrityError("compare-and-swap conflict")
                if revision != len(existing) or record.revision != revision:
                    raise ResourceAuthorizationIntegrityError("revision gap")
                assert_authorization_transition(existing, record)
                await self._conn.execute(
                    "INSERT INTO assurance_resource_authorizations ("
                    "revision, schema_version, fencing_token, record_digest, payload"
                    ") VALUES (?, ?, ?, ?, ?)",
                    (
                        record.revision,
                        RESOURCE_AUTHORIZATION_SCHEMA_VERSION,
                        record.fencing_token,
                        record.record_digest,
                        canonical_json_bytes(record.canonical_projection()),
                    ),
                )
                await self._conn.commit()
            except BaseException:
                await self._conn.rollback()
                raise

    async def read_records(self) -> tuple[ResourceAuthorizationRecord, ...]:
        async with self._lock:
            return tuple(await self._load_records_unlocked())

    async def assert_current_fence(self, authorization_id: str, fencing_token: int) -> None:
        token = _fencing_token(fencing_token)
        current = active_authorization_grant(await self.read_records(), authorization_id)
        if current is None or current.fencing_token != token:
            raise StaleFencingToken("fencing token is stale")

    async def _load_records_unlocked(self) -> list[ResourceAuthorizationRecord]:
        cursor = await self._conn.execute(
            "SELECT revision, schema_version, fencing_token, record_digest, payload "
            "FROM assurance_resource_authorizations ORDER BY revision"
        )
        rows = await cursor.fetchall()
        records: list[ResourceAuthorizationRecord] = []
        for index, row in enumerate(rows):
            if int(row[0]) != index:
                raise ResourceAuthorizationIntegrityError("revision gap")
            records.append(
                decode_resource_authorization_record(
                    json.loads(bytes(row[4] or b"").decode("utf-8")),
                    schema_version=str(row[1]),
                    record_digest=str(row[3]),
                )
            )
        return records


__all__ = ["SqliteResourceAuthorizationStore"]
