import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from assurance_product.change_workspace import ChangeWorkspace
from assurance_product.sqlite_attempt_checkpoint import SqliteAttemptCheckpointStore
from assurance_product.sqlite_checkpointer import open_sqlite_checkpointer
from graph_engine.attempts.checkpoint import AttemptCheckpoint, AttemptPhase, AttemptResult
from graph_engine.attempts.keys import AttemptKey
from graph_engine.canonical import canonical_digest
from graph_engine.persistence.attempt_checkpoint import AttemptCheckpointIntegrityError
from graph_engine.persistence.runner_lease import StaleFencingToken


def record(label="first", token=1):
    return AttemptCheckpoint(
        attempt_key=AttemptKey(digest=canonical_digest(label)),
        revision=0,
        fencing_token=token,
        phase=AttemptPhase.AUTHORIZE,
        contract_digest="b" * 64,
        input_digest="c" * 64,
        graph_revision="d" * 64,
        invocation_id="inv-1",
        public_entrypoint="execute",
        semantic_node_id="execution.run",
    )


@pytest.fixture
def workspace(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    return ChangeWorkspace.prepare(project, "CH-1")


def test_reconstruct_store_preserves_phase_all_attempts_and_generation_input(workspace):
    async def scenario():
        scope = {"invocation_id": "inv-1", "semantic_node_id": "node"}
        async with open_sqlite_checkpointer(workspace) as backend:
            owned = await backend.lease.acquire("inv-1", owner_id="worker")
            store = SqliteAttemptCheckpointStore(backend)
            await store.register_generation(
                scope, lambda n: record(str(n)).attempt_key, max_attempts=2, validated_input={"saved": [1]}
            )
            failed = replace(
                record("failed"),
                phase=AttemptPhase.RELEASE,
                terminal=AttemptResult(
                    resolution_kind="permanent", failure_kind="internal", message="old failure"
                ),
            )
            await store.commit(failed, expected_revision=0, fencing_token=1)
            saved = replace(
                record(),
                phase=AttemptPhase.RECONCILE,
                authorization_id="e" * 64,
                activity_id="call",
                activity_state="dispatch_started",
                activity_dispatch_fingerprint={"request": "one"},
                activity_dispatch_fingerprint_digest=canonical_digest({"request": "one"}),
            )
            await store.commit(saved, expected_revision=0, fencing_token=1)
            await store.ensure_durable(saved.attempt_key)
            await backend.lease.release(owned)
        async with open_sqlite_checkpointer(workspace) as backend:
            store = SqliteAttemptCheckpointStore(backend)
            loaded = await store.load(record().attempt_key)
            assert loaded is not None
            assert loaded.phase is AttemptPhase.RECONCILE
            assert loaded.activity_dispatch_fingerprint == {"request": "one"}
            failed = await store.load(record("failed").attempt_key)
            assert failed is not None and failed.terminal is not None
            assert failed.terminal.message == "old failure"
            assert len(await store.read_checkpoints()) == 2
            generation = await store.latest_generation(scope)
            assert generation is not None
            assert generation[3] == {"saved": [1]}
            assert (
                await store.register_generation(scope, lambda n: record(str(n)).attempt_key, max_attempts=2)
                is not None
            )
            assert (
                await store.register_generation(scope, lambda n: record(str(n)).attempt_key, max_attempts=2)
                is None
            )

    asyncio.run(scenario())


def test_conflicts_and_live_fence_rollback_leave_valid_bytes(workspace):
    async def scenario():
        async with open_sqlite_checkpointer(workspace) as backend:
            owned = await backend.lease.acquire("inv-1", owner_id="old")
            store = SqliteAttemptCheckpointStore(backend)
            saved = await store.commit(record(), expected_revision=0, fencing_token=1)
            with pytest.raises(AttemptCheckpointIntegrityError, match="compare-and-swap"):
                await store.commit(record(), expected_revision=0, fencing_token=1)
            with pytest.raises(AttemptCheckpointIntegrityError, match="identity"):
                await store.commit(
                    replace(saved, input_digest="e" * 64), expected_revision=1, fencing_token=1
                )
            await backend.lease.release(owned)
            newer = await backend.lease.acquire("inv-1", owner_id="new")
            with pytest.raises(StaleFencingToken):
                await store.commit(saved, expected_revision=1, fencing_token=1)
            assert await store.load(saved.attempt_key) == saved
            updated = await store.commit(
                replace(saved, fencing_token=2), expected_revision=1, fencing_token=2
            )
            assert updated.revision == 2
            await backend.lease.release(newer)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "column,value",
    [
        ("payload", b"{}"),
        ("revision", 99),
        ("fencing_token", 99),
        ("schema_version", "bad"),
        ("record_digest", "0" * 64),
        ("attempt_key_digest", "0" * 64),
    ],
)
def test_corrupted_rows_are_never_loaded_or_replaced(workspace, column, value):
    async def scenario():
        async with open_sqlite_checkpointer(workspace) as backend:
            owned = await backend.lease.acquire("inv-1", owner_id="worker")
            store = SqliteAttemptCheckpointStore(backend)
            saved = await store.commit(record(), expected_revision=0, fencing_token=1)
            await backend._conn.execute(f"UPDATE assurance_attempt_checkpoints SET {column} = ?", (value,))
            await backend._conn.commit()
            with pytest.raises(AttemptCheckpointIntegrityError):
                await store.read_checkpoints()
            if column != "attempt_key_digest":
                with pytest.raises(AttemptCheckpointIntegrityError):
                    await store.commit(saved, expected_revision=1, fencing_token=1)
            await backend.lease.release(owned)

    asyncio.run(scenario())


def test_new_store_refuses_old_journal_without_changing_its_bytes(workspace):
    from assurance_product.sqlite_attempt_store import SqliteAttemptJournal
    from graph_engine.attempts.events import AttemptOpened

    async def scenario():
        async with open_sqlite_checkpointer(workspace) as backend:
            await SqliteAttemptJournal(backend).append(
                record().attempt_key,
                (
                    AttemptOpened(
                        contract_digest="b" * 64,
                        input_digest="c" * 64,
                        graph_revision="d" * 64,
                        invocation_id="inv-1",
                        public_entrypoint="execute",
                        semantic_node_id="execution.run",
                    ),
                ),
                expected_revision=0,
                fencing_token=1,
            )
            cursor = await backend._conn.execute("SELECT payload FROM assurance_attempt_batches")
            before = await cursor.fetchone()
            store = SqliteAttemptCheckpointStore(backend)
            with pytest.raises(AttemptCheckpointIntegrityError, match="fresh isolated run"):
                await store.load(record().attempt_key)
            with pytest.raises(AttemptCheckpointIntegrityError, match="fresh isolated run"):
                await store.register_generation(
                    {"invocation_id": "inv-1"}, lambda n: record().attempt_key, max_attempts=1
                )
            cursor = await backend._conn.execute("SELECT payload FROM assurance_attempt_batches")
            assert await cursor.fetchone() == before
            cursor = await backend._conn.execute("SELECT count(*) FROM assurance_attempt_checkpoints")
            assert (await cursor.fetchone())[0] == 0

    asyncio.run(scenario())
