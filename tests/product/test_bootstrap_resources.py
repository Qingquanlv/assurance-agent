from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from graph_engine.canonical import canonical_digest, canonical_json_bytes
from graph_engine.persistence.resource_authorization import (
    RESOURCE_AUTHORIZATION_SCHEMA_VERSION,
    ResourceAuthorizationRecord,
    active_authorization_grant,
    decode_resource_authorization_record,
)
from graph_engine.plugin_api import ResourceClaims

from assurance_product.bootstrap.resources import release_active_resource_authorizations


def _authorization_id(attempt_key_digest: str, claims: ResourceClaims) -> str:
    return canonical_digest(
        {
            "attempt_key": attempt_key_digest,
            "claims": claims.model_dump(mode="json"),
        }
    )


def test_release_active_resource_authorizations_clears_stale_grant(tmp_path: Path) -> None:
    db_path = tmp_path / "checkpoints.sqlite3"
    claims = ResourceClaims(reads=("qa",), writes=("qa/.qa.yaml",))
    attempt = "a" * 64
    acquire = ResourceAuthorizationRecord.build(
        revision=0,
        action="acquire",
        authorization_id=_authorization_id(attempt, claims),
        attempt_key_digest=attempt,
        fencing_token=2,
        claims=claims,
    )
    connection = sqlite3.connect(str(db_path))
    connection.execute(
        "CREATE TABLE assurance_resource_authorizations ("
        "revision INTEGER PRIMARY KEY, schema_version TEXT NOT NULL, "
        "fencing_token INTEGER NOT NULL, record_digest TEXT NOT NULL, payload BLOB NOT NULL)"
    )
    connection.execute(
        "INSERT INTO assurance_resource_authorizations ("
        "revision, schema_version, fencing_token, record_digest, payload"
        ") VALUES (?, ?, ?, ?, ?)",
        (
            acquire.revision,
            RESOURCE_AUTHORIZATION_SCHEMA_VERSION,
            acquire.fencing_token,
            acquire.record_digest,
            canonical_json_bytes(acquire.canonical_projection()),
        ),
    )
    connection.commit()
    connection.close()

    assert release_active_resource_authorizations(db_path) == 1
    assert release_active_resource_authorizations(db_path) == 0

    connection = sqlite3.connect(str(db_path))
    rows = connection.execute(
        "SELECT revision, schema_version, record_digest, payload "
        "FROM assurance_resource_authorizations ORDER BY revision"
    ).fetchall()
    connection.close()
    records = [
        decode_resource_authorization_record(
            json.loads(bytes(row[3] or b"").decode("utf-8")),
            schema_version=str(row[1]),
            record_digest=str(row[2]),
        )
        for row in rows
    ]
    assert len(records) == 2
    assert records[1].action == "release"
    assert active_authorization_grant(records, acquire.authorization_id) is None
