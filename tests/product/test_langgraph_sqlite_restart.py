from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from assurance_product import open_sqlite_checkpointer
from assurance_product.change_workspace import ChangeWorkspace
from graph_engine.application import AssuranceApplication, AssuranceRuntimeContext, InvocationStatus
from graph_engine.boot.graph_revision import BootArtifact, GraphBuildManifest, GraphRevision
from graph_engine.canonical import canonical_digest
from graph_engine.persistence.journal import InvocationStarted


LOCK = "b" * 64
INPUT_DIGEST = canonical_digest({"change_id": "chg-1"})


class InterruptState(TypedDict, total=False):
    change_id: str
    visits: int
    phase: str


def _workspace(tmp_path: Path) -> ChangeWorkspace:
    project = (tmp_path / "project").resolve()
    (project / "qa" / "changes" / "CH-1").mkdir(parents=True)
    return ChangeWorkspace.prepare(project, "CH-1")


def _revision() -> GraphRevision:
    return GraphRevision.build(
        product_lock_digest=LOCK,
        wheel_source_digests={"assurance.product": "d" * 64},
        factory_symbols=("assurance_product.graphs.factory:build_product_graphs",),
        state_schema_versions={"execute": "1"},
        langgraph_version="1.2.11",
        checkpoint_contract_version="1",
    )


def _artifact(graph: object) -> BootArtifact:
    revision = _revision()
    return BootArtifact(
        manifest=GraphBuildManifest(
            revision=revision,
            entrypoint_contract_digests={"execute": canonical_digest({"entrypoint": "execute"})},
            attempt_contract_digests={},
        ),
        entrypoints={"execute": graph},  # type: ignore[arg-type]
        attempt_contracts={},
        checkpointer_backend_id="anchored",
    )


def _context(artifact: BootArtifact) -> AssuranceRuntimeContext:
    return AssuranceRuntimeContext(
        revision_id=artifact.manifest.revision.revision_id,
        fencing_token=1,
        attempt_kernel=object(),
        secret_resolver=object(),
        workspace_provider=object(),
    )


def _thread_id(config: object) -> str:
    document = config if isinstance(config, dict) else {}
    configurable = document.get("configurable") or {}
    return str(configurable["thread_id"])


def _graph(saver: BaseCheckpointSaver[int]):
    def prepare(state: InterruptState) -> dict[str, object]:
        return {"visits": int(state.get("visits") or 0) + 1, "phase": "prepared"}

    def work(state: InterruptState) -> dict[str, str]:
        interrupt({"kind": "human", "actions": ["approve", "reject"]})
        return {"phase": "done"}

    builder = StateGraph(InterruptState)
    builder.add_node("prepare", prepare)
    builder.add_node("work", work)
    builder.add_edge(START, "prepare")
    builder.add_edge("prepare", "work")
    builder.add_edge("work", END)
    return builder.compile(checkpointer=saver)


def test_process_reopen_resumes_same_invocation_without_duplicating_prepare(
    tmp_path: Path,
) -> None:
    asyncio.run(_process_reopen_resumes_same_invocation_without_duplicating_prepare(tmp_path))


async def _process_reopen_resumes_same_invocation_without_duplicating_prepare(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    async with open_sqlite_checkpointer(workspace) as first:
        revision = _revision()
        started = InvocationStarted(
            invocation_id="inv-1",
            thread_id="inv-1",
            graph_revision=revision.revision_id,
            product_lock_digest=revision.product_lock_digest,
            root_input_digest=INPUT_DIGEST,
            fencing_token=1,
        )
        await first.journal.start_invocation(started, fencing_token=1)
        await first.remember_entrypoint("inv-1", "execute")
        saver = first.checkpointer(started.anchor_state())
        artifact = _artifact(_graph(saver))
        application = AssuranceApplication(lease=first.lease, owner_id="runner-a")
        pinned = await application.start(
            artifact=artifact,
            invocation_id="inv-1",
            entrypoint="execute",
            graph_input={"change_id": "chg-1"},
            runtime_context=_context(artifact),
        )
        assert pinned.thread_id == pinned.invocation_id == "inv-1"
        interrupted = await application.run(
            artifact=artifact,
            invocation_id="inv-1",
            runtime_context=_context(artifact),
        )
        assert interrupted.status == "interrupted"
        snapshot = await artifact.entrypoints["execute"].aget_state({"configurable": {"thread_id": "inv-1"}})
        assert snapshot.values["visits"] == 1
        assert snapshot.values["phase"] == "prepared"
        assert _thread_id(snapshot.config) == "inv-1"

    async with open_sqlite_checkpointer(workspace) as second:
        recovered = await second.recover_handshake("inv-1")
        assert recovered is not None
        assert recovered.thread_id == recovered.invocation_id == "inv-1"
        assert await second.read_entrypoint("inv-1") == "execute"
        saver = second.checkpointer(recovered.anchor_state())
        await saver.arecover(thread_id="inv-1")
        artifact = _artifact(_graph(saver))
        application = AssuranceApplication(lease=second.lease, owner_id="runner-b")
        result = await application.resume(
            artifact=artifact,
            invocation_id="inv-1",
            runtime_context=_context(artifact),
            resume="approve",
        )
        assert result == InvocationStatus(status="completed")
        snapshot = await artifact.entrypoints["execute"].aget_state({"configurable": {"thread_id": "inv-1"}})
        assert snapshot.values["visits"] == 1
        assert snapshot.values["phase"] == "done"
        assert snapshot.next == ()
        assert _thread_id(snapshot.config) == "inv-1"
