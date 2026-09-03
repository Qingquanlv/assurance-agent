from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from assurance_product.change_workspace import ChangeWorkspace
from assurance_product.sqlite_checkpointer import open_sqlite_checkpointer
from assurance_product.sqlite_resource_authorization import SqliteResourceAuthorizationStore
from graph_engine.canonical import canonical_digest
from graph_engine.persistence.resource_authorization import (
    ResourceAuthorizationAction,
    ResourceAuthorizationIntegrityError,
    ResourceAuthorizationRecord,
)
from graph_engine.persistence.runner_lease import StaleFencingToken
from graph_engine.plugin_api import ResourceClaims


def _workspace(tmp_path: Path) -> ChangeWorkspace:
    project = (tmp_path / "project").resolve()
    (project / "qa" / "changes" / "CH-1").mkdir(parents=True)
    return ChangeWorkspace.prepare(project, "CH-1")


def _claims() -> ResourceClaims:
    return ResourceClaims(writes=("qa/a",))


def _authorization_id(*, label: str = "holder") -> str:
    return canonical_digest(
        {
            "attempt_key": "a" * 64,
            "claims": _claims().model_dump(mode="json"),
            "label": label,
        }
    )


def _record(
    *,
    revision: int,
    action: ResourceAuthorizationAction,
    fencing_token: int,
    label: str = "holder",
) -> ResourceAuthorizationRecord:
    return ResourceAuthorizationRecord.build(
        revision=revision,
        action=action,
        authorization_id=_authorization_id(label=label),
        attempt_key_digest="a" * 64,
        fencing_token=fencing_token,
        claims=_claims(),
    )


async def append_acquire(
    store: SqliteResourceAuthorizationStore, *, fence: int
) -> ResourceAuthorizationRecord:
    record = _record(revision=0, action="acquire", fencing_token=fence)
    await store.append(record, expected_revision=0, fencing_token=fence)
    return record


async def append_release(
    store: SqliteResourceAuthorizationStore, *, fence: int
) -> ResourceAuthorizationRecord:
    record = _record(revision=1, action="release", fencing_token=fence)
    await store.append(record, expected_revision=1, fencing_token=fence)
    return record


@pytest.fixture
def workspace(tmp_path: Path) -> ChangeWorkspace:
    return _workspace(tmp_path)


def test_resource_store_rejects_stale_release(tmp_path: Path) -> None:
    asyncio.run(_resource_store_rejects_stale_release(tmp_path))


async def _resource_store_rejects_stale_release(tmp_path: Path) -> None:
    async with open_sqlite_checkpointer(_workspace(tmp_path)) as backend:
        sqlite_resource_store = SqliteResourceAuthorizationStore(backend)
        await append_acquire(sqlite_resource_store, fence=4)
        with pytest.raises(StaleFencingToken):
            await append_release(sqlite_resource_store, fence=3)


def test_resource_store_survives_backend_restart(workspace) -> None:
    asyncio.run(_resource_store_survives_backend_restart(workspace))


async def _resource_store_survives_backend_restart(workspace) -> None:
    async with open_sqlite_checkpointer(workspace) as backend:
        store = SqliteResourceAuthorizationStore(backend)
        acquire = await append_acquire(store, fence=4)
        adopt = _record(revision=1, action="adopt", fencing_token=5)
        await store.append(adopt, expected_revision=1, fencing_token=5)
    async with open_sqlite_checkpointer(workspace) as reopened:
        loaded = await SqliteResourceAuthorizationStore(reopened).read_records()
        assert [record.action for record in loaded] == ["acquire", "adopt"]
        assert loaded[0] == acquire
        assert loaded[1].fencing_token == 5
        await SqliteResourceAuthorizationStore(reopened).assert_current_fence(acquire.authorization_id, 5)
        with pytest.raises(StaleFencingToken):
            await SqliteResourceAuthorizationStore(reopened).assert_current_fence(acquire.authorization_id, 4)


def test_identical_cas_append_is_idempotent(workspace) -> None:
    asyncio.run(_identical_cas_append_is_idempotent(workspace))


async def _identical_cas_append_is_idempotent(workspace) -> None:
    async with open_sqlite_checkpointer(workspace) as backend:
        store = SqliteResourceAuthorizationStore(backend)
        record = _record(revision=0, action="acquire", fencing_token=4)
        await store.append(record, expected_revision=0, fencing_token=4)
        await store.append(record, expected_revision=0, fencing_token=4)
        assert await store.read_records() == (record,)


def test_divergent_record_at_same_revision_fails_closed(workspace) -> None:
    asyncio.run(_divergent_record_at_same_revision_fails_closed(workspace))


async def _divergent_record_at_same_revision_fails_closed(workspace) -> None:
    async with open_sqlite_checkpointer(workspace) as backend:
        store = SqliteResourceAuthorizationStore(backend)
        first = _record(revision=0, action="acquire", fencing_token=4)
        await store.append(first, expected_revision=0, fencing_token=4)
        with pytest.raises(ResourceAuthorizationIntegrityError):
            await store.append(
                _record(revision=0, action="acquire", fencing_token=4, label="other"),
                expected_revision=0,
                fencing_token=4,
            )
        assert await store.read_records() == (first,)


def test_revision_gap_is_rejected(workspace) -> None:
    asyncio.run(_revision_gap_is_rejected(workspace))


async def _revision_gap_is_rejected(workspace) -> None:
    async with open_sqlite_checkpointer(workspace) as backend:
        store = SqliteResourceAuthorizationStore(backend)
        with pytest.raises(ResourceAuthorizationIntegrityError, match="gap"):
            await store.append(
                _record(revision=2, action="acquire", fencing_token=4),
                expected_revision=2,
                fencing_token=4,
            )


def test_authorizations_table_stores_canonical_bytes(workspace) -> None:
    asyncio.run(_authorizations_table_stores_canonical_bytes(workspace))


async def _authorizations_table_stores_canonical_bytes(workspace) -> None:
    async with open_sqlite_checkpointer(workspace) as backend:
        store = SqliteResourceAuthorizationStore(backend)
        await append_acquire(store, fence=4)
    with sqlite3.connect(workspace.paths.langgraph_checkpoints) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(assurance_resource_authorizations)")}
        assert {
            "revision",
            "schema_version",
            "fencing_token",
            "record_digest",
            "payload",
        } <= columns
        row = conn.execute(
            "SELECT revision, schema_version, fencing_token, record_digest "
            "FROM assurance_resource_authorizations"
        ).fetchone()
        assert row is not None
        assert row[0] == 0
        assert row[1] == "1"
        assert row[2] == 4
        assert len(row[3]) == 64
