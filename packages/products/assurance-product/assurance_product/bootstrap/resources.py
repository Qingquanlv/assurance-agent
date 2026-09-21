from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from graph_engine.canonical import canonical_json_bytes
from graph_engine.persistence.resource_authorization import (
    RESOURCE_AUTHORIZATION_SCHEMA_VERSION,
    ResourceAuthorizationRecord,
    assert_authorization_transition,
    decode_resource_authorization_record,
)


def release_active_resource_authorizations(db_path: Path) -> int:
    if not db_path.is_file():
        return 0
    connection = sqlite3.connect(str(db_path), timeout=30)
    try:
        connection.execute("BEGIN IMMEDIATE")
        rows = connection.execute(
            "SELECT revision, schema_version, fencing_token, record_digest, payload "
            "FROM assurance_resource_authorizations ORDER BY revision"
        ).fetchall()
        records = [
            decode_resource_authorization_record(
                json.loads(bytes(row[4] or b"").decode("utf-8")),
                schema_version=str(row[1]),
                record_digest=str(row[3]),
            )
            for row in rows
        ]
        released = 0
        for grant in _active_grants(records):
            record = ResourceAuthorizationRecord.build(
                revision=len(records),
                action="release",
                authorization_id=grant.authorization_id,
                attempt_key_digest=grant.attempt_key_digest,
                fencing_token=grant.fencing_token,
                claims=grant.claims,
            )
            assert_authorization_transition(records, record)
            connection.execute(
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
            records.append(record)
            released += 1
        connection.commit()
        return released
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def _active_grants(
    records: list[ResourceAuthorizationRecord],
) -> tuple[ResourceAuthorizationRecord, ...]:
    grants: dict[str, ResourceAuthorizationRecord] = {}
    for record in records:
        if record.action in {"acquire", "adopt"}:
            grants[record.authorization_id] = record
        elif record.action == "release":
            grants.pop(record.authorization_id, None)
    return tuple(grants.values())
