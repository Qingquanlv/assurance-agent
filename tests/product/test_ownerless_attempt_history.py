from __future__ import annotations

import asyncio
from pathlib import Path

import pytest


@pytest.mark.parametrize("history", ["checkpoint", "generation", "call", "legacy"])
def test_missing_owner_metadata_refuses_retained_execution_history(tmp_path: Path, history: str) -> None:
    from assurance_product.worker_lifecycle import ExecutionConflict, acquire_execution

    asyncio.run(_seed_history(tmp_path, history))
    with pytest.raises(ExecutionConflict, match="no verifiable owner"):
        with acquire_execution(tmp_path, "replacement"):
            pytest.fail("ownerless earlier execution history was admitted")


def test_empty_checkpoint_database_allows_new_execution_owner(tmp_path: Path) -> None:
    from assurance_product.worker_lifecycle import acquire_execution

    asyncio.run(_seed_history(tmp_path, "empty"))
    with acquire_execution(tmp_path, "new") as owner:
        assert owner.invocation == "new"


async def _seed_history(project: Path, history: str) -> None:
    from assurance_product.change_workspace import ChangeWorkspace
    from assurance_product.sqlite_attempt_checkpoint import SqliteAttemptCheckpointStore
    from assurance_product.sqlite_checkpointer import open_sqlite_checkpointer
    from graph_engine.attempts.orchestration.checkpoint import AttemptPhase
    from graph_engine.canonical import JSONValue, canonical_digest
    from tests.attempt_checkpoints import checkpoint

    workspace = ChangeWorkspace.prepare(project.resolve(), "CH-1")
    async with open_sqlite_checkpointer(workspace) as backend:
        lease = await backend.lease.acquire("inv-1", owner_id="previous-worker")
        try:
            store = SqliteAttemptCheckpointStore(backend)
            if history == "checkpoint":
                reference: JSONValue = {"session_id": "external-session"}
                fingerprint: JSONValue = {"request": "previous-request"}
                bound = checkpoint(
                    fencing_token=lease.fencing_token,
                    phase=AttemptPhase.RECONCILE,
                    authorization_id="e" * 64,
                    activity_id="call",
                    activity_state="bound",
                    activity_reference=reference,
                    activity_reference_digest=canonical_digest(reference),
                    activity_dispatch_fingerprint=fingerprint,
                    activity_dispatch_fingerprint_digest=canonical_digest(fingerprint),
                )
                saved = await store.commit(bound, expected_revision=0, fencing_token=lease.fencing_token)
                assert saved.activity_state == "bound"
            elif history == "generation":
                await store.register_generation(
                    {"invocation_id": "inv-1"},
                    lambda ordinal: checkpoint().attempt_key,
                    max_attempts=1,
                )
            elif history == "call":
                await backend._conn.execute(
                    "INSERT INTO assurance_host_calls (call_digest, owner_nonce, attempt_key_digest, payload) "
                    "VALUES (?, ?, ?, ?)",
                    ("a" * 64, "lost-owner", "b" * 64, b"retained-call-evidence"),
                )
                await backend._conn.commit()
            elif history == "legacy":
                await backend._conn.execute("CREATE TABLE assurance_attempt_batches (payload BLOB NOT NULL)")
                await backend._conn.execute("INSERT INTO assurance_attempt_batches VALUES (?)", (b"legacy",))
                await backend._conn.commit()
        finally:
            await backend.lease.release(lease)
