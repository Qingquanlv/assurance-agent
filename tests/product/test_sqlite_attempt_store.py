from __future__ import annotations

import asyncio
import inspect
import sqlite3
from pathlib import Path

import pytest

from assurance_product.change_workspace import ChangeWorkspace
from assurance_product.sqlite_attempt_store import SqliteAttemptJournal
from assurance_product.sqlite_checkpointer import open_sqlite_checkpointer
from graph_engine.attempts.events import AttemptOpened, ResourcesAuthorized
from graph_engine.attempts.keys import AttemptKey
from graph_engine.canonical import canonical_digest
from graph_engine.persistence.attempt_journal import AttemptJournalIntegrityError
from graph_engine.persistence.runner_lease import StaleFencingToken


@pytest.fixture
def workspace(tmp_path: Path) -> ChangeWorkspace:
    project = (tmp_path / "project").resolve()
    (project / "qa").mkdir(parents=True)
    return ChangeWorkspace.prepare(project, "CH-1")


@pytest.fixture
def attempt_key() -> AttemptKey:
    return AttemptKey(digest="a" * 64)


def _opened() -> AttemptOpened:
    return AttemptOpened(
        contract_digest="b" * 64,
        input_digest="c" * 64,
        graph_revision="d" * 64,
        invocation_id="inv-1",
        public_entrypoint="intake",
        semantic_node_id="intake.explore",
    )


def test_attempt_journal_survives_backend_restart(workspace, attempt_key) -> None:
    asyncio.run(_attempt_journal_survives_backend_restart(workspace, attempt_key))


async def _attempt_journal_survives_backend_restart(workspace, attempt_key) -> None:
    async with open_sqlite_checkpointer(workspace) as backend:
        journal = SqliteAttemptJournal(backend)
        await journal.append(
            attempt_key,
            (
                AttemptOpened(
                    contract_digest="b" * 64,
                    input_digest="c" * 64,
                    graph_revision="d" * 64,
                    invocation_id="inv-1",
                    public_entrypoint="intake",
                    semantic_node_id="intake.explore",
                ),
            ),
            expected_revision=0,
            fencing_token=3,
        )
        await journal.ensure_durable(attempt_key)
    async with open_sqlite_checkpointer(workspace) as reopened:
        snapshot = await SqliteAttemptJournal(reopened).load(attempt_key)
        assert snapshot is not None
        assert snapshot.revision == 1
        assert snapshot.fencing_token == 3


def test_identical_append_replay_is_idempotent(workspace, attempt_key) -> None:
    asyncio.run(_identical_append_replay_is_idempotent(workspace, attempt_key))


async def _identical_append_replay_is_idempotent(workspace, attempt_key) -> None:
    async with open_sqlite_checkpointer(workspace) as backend:
        journal = SqliteAttemptJournal(backend)
        batch = (_opened(),)
        first = await journal.append(attempt_key, batch, expected_revision=0, fencing_token=4)
        second = await journal.append(attempt_key, batch, expected_revision=0, fencing_token=4)
        loaded = await journal.load(attempt_key)
        assert first == second
        assert loaded == first
        assert loaded is not None
        assert loaded.revision == 1
        assert loaded.fencing_token == 4


def test_divergent_batch_at_same_revision_conflicts(workspace, attempt_key) -> None:
    asyncio.run(_divergent_batch_at_same_revision_conflicts(workspace, attempt_key))


async def _divergent_batch_at_same_revision_conflicts(workspace, attempt_key) -> None:
    async with open_sqlite_checkpointer(workspace) as backend:
        journal = SqliteAttemptJournal(backend)
        await journal.append(attempt_key, (_opened(),), expected_revision=0, fencing_token=4)
        with pytest.raises(AttemptJournalIntegrityError, match="compare-and-swap"):
            await journal.append(
                attempt_key,
                (
                    AttemptOpened(
                        contract_digest=canonical_digest({"other": "contract"}),
                        input_digest="c" * 64,
                        graph_revision="d" * 64,
                        invocation_id="inv-1",
                        public_entrypoint="intake",
                        semantic_node_id="intake.explore",
                    ),
                ),
                expected_revision=0,
                fencing_token=4,
            )
        loaded = await journal.load(attempt_key)
        assert loaded is not None
        assert loaded.contract_digest == "b" * 64


def test_revision_gap_is_rejected(workspace, attempt_key) -> None:
    asyncio.run(_revision_gap_is_rejected(workspace, attempt_key))


async def _revision_gap_is_rejected(workspace, attempt_key) -> None:
    async with open_sqlite_checkpointer(workspace) as backend:
        journal = SqliteAttemptJournal(backend)
        with pytest.raises(AttemptJournalIntegrityError, match="gap"):
            await journal.append(attempt_key, (_opened(),), expected_revision=2, fencing_token=4)


def test_stale_fence_cannot_append_after_newer_owner(workspace, attempt_key) -> None:
    asyncio.run(_stale_fence_cannot_append_after_newer_owner(workspace, attempt_key))


async def _stale_fence_cannot_append_after_newer_owner(workspace, attempt_key) -> None:
    async with open_sqlite_checkpointer(workspace) as backend:
        journal = SqliteAttemptJournal(backend)
        await journal.append(attempt_key, (_opened(),), expected_revision=0, fencing_token=4)
        await journal.append(
            attempt_key,
            (ResourcesAuthorized(authorization_id=canonical_digest({"auth": "1"})),),
            expected_revision=1,
            fencing_token=5,
        )
        with pytest.raises(StaleFencingToken):
            await journal.append(
                attempt_key,
                (ResourcesAuthorized(authorization_id=canonical_digest({"auth": "2"})),),
                expected_revision=2,
                fencing_token=4,
            )


def test_attempt_batches_table_stores_canonical_bytes(workspace, attempt_key) -> None:
    asyncio.run(_attempt_batches_table_stores_canonical_bytes(workspace, attempt_key))


async def _attempt_batches_table_stores_canonical_bytes(workspace, attempt_key) -> None:
    async with open_sqlite_checkpointer(workspace) as backend:
        journal = SqliteAttemptJournal(backend)
        await journal.append(attempt_key, (_opened(),), expected_revision=0, fencing_token=3)
    with sqlite3.connect(workspace.paths.langgraph_checkpoints) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(assurance_attempt_batches)")}
        assert {
            "attempt_key_digest",
            "revision",
            "schema_version",
            "fencing_token",
            "record_digest",
            "payload",
        } <= columns
        row = conn.execute(
            "SELECT attempt_key_digest, revision, schema_version, fencing_token, record_digest "
            "FROM assurance_attempt_batches"
        ).fetchone()
        assert row is not None
        assert row[0] == attempt_key.digest
        assert row[1] == 0
        assert row[2] == "1"
        assert row[3] == 3
        assert len(row[4]) == 64


def test_product_does_not_expose_pickle_attempt_journal() -> None:
    import assurance_product.runtime_ports as ports

    assert not hasattr(ports, "DurableAttemptJournal")
    source = inspect.getsource(ports)
    assert "attempts.pkl" not in source
    assert "import pickle" not in source


def test_generation_registration_consumes_budget_across_restart(workspace) -> None:
    async def scenario():
        scope = {"invocation_id": "inv-1", "semantic_node_id": "intake.explore", "activation": "root:1"}

        def key(ordinal):
            return AttemptKey(digest=canonical_digest({"ordinal": ordinal}))

        async with open_sqlite_checkpointer(workspace) as backend:
            journal = SqliteAttemptJournal(backend)
            first = await journal.register_generation(scope, key, max_attempts=3)
            assert first is not None
            assert first[0] == 1
            second = await journal.register_generation(scope, key, max_attempts=3)
            assert second is not None
            assert second[0] == 2
        async with open_sqlite_checkpointer(workspace) as backend:
            journal = SqliteAttemptJournal(backend)
            third = await journal.register_generation(scope, key, max_attempts=3)
            assert third is not None
            assert third[0] == 3
            assert await journal.register_generation(scope, key, max_attempts=3) is None
            latest = await journal.latest_generation(scope)
            assert latest is not None
            assert latest[0] == 3

    asyncio.run(scenario())


def test_abandoned_system_resume_preserves_checkpoint_envelope(workspace) -> None:
    from types import SimpleNamespace
    from assurance_product.application import _abandoned_system_resume
    from assurance_product.sqlite_checkpointer import open_sqlite_checkpointer

    async def scenario():
        async with open_sqlite_checkpointer(workspace) as backend:
            journal = SqliteAttemptJournal(backend)
            scope = {"invocation_id": "inv-1", "semantic_node_id": "node"}
            registered = await journal.register_generation(
                scope, lambda value: AttemptKey(digest=canonical_digest({"ordinal": value})), max_attempts=3
            )
            assert registered is not None
            _ordinal, key = registered
            payload = {
                "kind": "system_block",
                "attempt_key": key.digest,
                "reconciliation": {"reference_id": "provider:unknown"},
            }

            class Graph:
                async def aget_state(self, config):
                    return SimpleNamespace(interrupts=(SimpleNamespace(id="interrupt", value=payload),))

            class Ports:
                def __init__(self):
                    self.backend = backend

                async def read_only_execution(self, **kwargs):
                    return SimpleNamespace(artifact=SimpleNamespace(entrypoints={"execute": Graph()}))

            ports = Ports()
            record = SimpleNamespace(entrypoint="execute", root_input_digest="d" * 64)
            assert await _abandoned_system_resume(ports, record, "inv-1") is None
            await backend._conn.execute("UPDATE assurance_attempt_generations SET abandoned = 1")
            await backend._conn.commit()
            assert await _abandoned_system_resume(ports, record, "inv-1") == {
                "interrupt": {"reconciliation": {"reference_id": "provider:unknown"}}
            }

    asyncio.run(scenario())
