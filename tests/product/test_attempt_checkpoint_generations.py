from __future__ import annotations
import asyncio
from types import SimpleNamespace
from tests.product.test_sqlite_attempt_checkpoint import workspace as workspace
from graph_engine.attempts.models.keys import AttemptKey
from graph_engine.canonical import canonical_digest
from assurance_product.sqlite_attempt_checkpoint import SqliteAttemptCheckpointStore
from assurance_product.sqlite_checkpointer import open_sqlite_checkpointer


def test_generation_registration_consumes_budget_across_restart(workspace) -> None:
    async def scenario():
        scope = {"invocation_id": "inv-1", "semantic_node_id": "intake.explore", "activation": "root:1"}

        def key(ordinal):
            return AttemptKey(digest=canonical_digest({"ordinal": ordinal}))

        async with open_sqlite_checkpointer(workspace) as backend:
            journal = SqliteAttemptCheckpointStore(backend)
            first = await journal.register_generation(scope, key, max_attempts=3)
            assert first is not None
            assert first[0] == 1
            second = await journal.register_generation(scope, key, max_attempts=3)
            assert second is not None
            assert second[0] == 2
        async with open_sqlite_checkpointer(workspace) as backend:
            journal = SqliteAttemptCheckpointStore(backend)
            third = await journal.register_generation(scope, key, max_attempts=3)
            assert third is not None
            assert third[0] == 3
            assert await journal.register_generation(scope, key, max_attempts=3) is None
            latest = await journal.latest_generation(scope)
            assert latest is not None
            assert latest[0] == 3

    asyncio.run(scenario())


def test_abandoned_system_resume_preserves_checkpoint_envelope(workspace) -> None:
    from assurance_product.application import _abandoned_system_resume
    from assurance_product.sqlite_checkpointer import open_sqlite_checkpointer

    async def scenario():
        async with open_sqlite_checkpointer(workspace) as backend:
            journal = SqliteAttemptCheckpointStore(backend)
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
