from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TypedDict

import pytest
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from assurance_product import open_sqlite_checkpointer
from assurance_product.change_workspace import ChangeWorkspace
from graph_engine.application import (
    AssuranceApplication,
    AssuranceRuntimeContext,
    InvocationBoundExecution,
    InvocationStatus,
)
from graph_engine.boot.graph_revision import BootArtifact, GraphBuildManifest, GraphRevision
from graph_engine.canonical import canonical_digest
from graph_engine.persistence.anchored_checkpointer import AnchoredCheckpointer
from graph_engine.persistence.journal import (
    CheckpointAnchorState,
    CheckpointIntegrityError,
    InvocationStarted,
)
from graph_engine.persistence.runner_lease import RunnerLease
from assurance_product.runtime_ports import _FenceAdvancedReplayJournal
from tests.product.unused_runtime_ports import (
    UNUSED_ATTEMPT_KERNEL,
    UNUSED_SECRET_RESOLVER,
    UNUSED_WORKSPACE_PROVIDER,
)


LOCK = "b" * 64
INPUT_DIGEST = canonical_digest({"change_id": "chg-1"})


class InterruptState(TypedDict, total=False):
    change_id: str
    visits: int
    phase: str


def _workspace(tmp_path: Path) -> ChangeWorkspace:
    project = (tmp_path / "project").resolve()
    (project / "qa").mkdir(parents=True)
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


class _LeaseBoundSaverFactory:
    def __init__(
        self,
        backend: object,
        *,
        revision: GraphRevision,
        root_input_digest: str,
        build_artifact,
    ) -> None:
        self._backend = backend
        self._revision = revision
        self._root_input_digest = root_input_digest
        self._build_artifact = build_artifact
        self.last: InvocationBoundExecution | None = None

    def bind(self, runner_lease: RunnerLease) -> InvocationBoundExecution:
        identity = CheckpointAnchorState(
            invocation_id="inv-1",
            thread_id="inv-1",
            graph_revision=self._revision.revision_id,
            product_lock_digest=self._revision.product_lock_digest,
            root_input_digest=self._root_input_digest,
            fencing_token=runner_lease.fencing_token,
        )
        saver = AnchoredCheckpointer(
            store=self._backend.store,  # type: ignore[attr-defined]
            journal=_FenceAdvancedReplayJournal(self._backend.journal),  # type: ignore[attr-defined]
            identity=identity,
            observers=self._backend.observers,  # type: ignore[attr-defined]
            lease=self._backend.lease,  # type: ignore[attr-defined]
            serde=self._backend.serializer,  # type: ignore[attr-defined]
        )
        artifact = self._build_artifact(saver)
        bound = InvocationBoundExecution(
            artifact=artifact,
            runtime_context=AssuranceRuntimeContext(
                revision_id=artifact.manifest.revision.revision_id,
                fencing_token=runner_lease.fencing_token,
                attempt_kernel=UNUSED_ATTEMPT_KERNEL,
                secret_resolver=UNUSED_SECRET_RESOLVER,
                workspace_provider=UNUSED_WORKSPACE_PROVIDER,
            ),
        )
        self.last = bound
        return bound


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
        first.seal_observers()
        factory = _LeaseBoundSaverFactory(
            first,
            revision=revision,
            root_input_digest=INPUT_DIGEST,
            build_artifact=lambda saver: _artifact(_graph(saver)),
        )
        application = AssuranceApplication(lease=first.lease, owner_id="runner-a")
        pinned = await application.start(
            invocation_id="inv-1",
            entrypoint="execute",
            graph_input={"change_id": "chg-1"},
            execution_factory=factory,
        )
        assert pinned.thread_id == pinned.invocation_id == "inv-1"
        interrupted = await application.run(
            invocation_id="inv-1",
            execution_factory=factory,
        )
        assert interrupted.status == "interrupted"
        assert factory.last is not None
        snapshot = await factory.last.artifact.entrypoints["execute"].aget_state(
            {"configurable": {"thread_id": "inv-1"}}
        )
        assert snapshot.values["visits"] == 1
        assert snapshot.values["phase"] == "prepared"
        assert _thread_id(snapshot.config) == "inv-1"

    async with open_sqlite_checkpointer(workspace) as second:
        recovered = await second.recover_handshake("inv-1")
        assert recovered is not None
        assert recovered.thread_id == recovered.invocation_id == "inv-1"
        assert await second.read_entrypoint("inv-1") == "execute"
        second.seal_observers()
        factory = _LeaseBoundSaverFactory(
            second,
            revision=revision,
            root_input_digest=INPUT_DIGEST,
            build_artifact=lambda saver: _artifact(_graph(saver)),
        )
        application = AssuranceApplication(lease=second.lease, owner_id="runner-b")
        result = await application.resume(
            invocation_id="inv-1",
            execution_factory=factory,
            resume="approve",
        )
        assert result == InvocationStatus(status="completed")
        assert factory.last is not None
        snapshot = await factory.last.artifact.entrypoints["execute"].aget_state(
            {"configurable": {"thread_id": "inv-1"}}
        )
        assert snapshot.values["visits"] == 1
        assert snapshot.values["phase"] == "done"
        assert snapshot.next == ()
        assert _thread_id(snapshot.config) == "inv-1"


def _idle_graph(saver: BaseCheckpointSaver[int]):
    def execute(state: InterruptState) -> InterruptState:
        return state

    builder = StateGraph(InterruptState)
    builder.add_node("execute", execute)
    builder.add_edge(START, "execute")
    builder.add_edge("execute", END)
    return builder.compile(checkpointer=saver)


def _multi_revision(*, lock: str = LOCK) -> GraphRevision:
    return GraphRevision.build(
        product_lock_digest=lock,
        wheel_source_digests={"assurance.product": "d" * 64},
        factory_symbols=("assurance_product.graphs.factory:build_product_graphs",),
        state_schema_versions={"execute": "1", "other": "1"},
        langgraph_version="1.2.11",
        checkpoint_contract_version="1",
    )


def _multi_artifact(
    execute_graph: object,
    other_graph: object,
    *,
    revision: GraphRevision | None = None,
) -> BootArtifact:
    revision = revision or _multi_revision()
    return BootArtifact(
        manifest=GraphBuildManifest(
            revision=revision,
            entrypoint_contract_digests={
                "execute": canonical_digest({"entrypoint": "execute"}),
                "other": canonical_digest({"entrypoint": "other"}),
            },
            attempt_contract_digests={},
        ),
        entrypoints={"execute": execute_graph, "other": other_graph},  # type: ignore[arg-type]
        attempt_contracts={},
        checkpointer_backend_id="anchored",
    )


def test_second_application_start_cannot_rewrite_lock_input_revision_or_entrypoint(
    tmp_path: Path,
) -> None:
    asyncio.run(_second_application_start_cannot_rewrite_lock_input_revision_or_entrypoint(tmp_path))


async def _second_application_start_cannot_rewrite_lock_input_revision_or_entrypoint(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    async with open_sqlite_checkpointer(workspace) as backend:
        revision = _multi_revision()
        started = InvocationStarted(
            invocation_id="inv-1",
            thread_id="inv-1",
            graph_revision=revision.revision_id,
            product_lock_digest=revision.product_lock_digest,
            root_input_digest=INPUT_DIGEST,
            fencing_token=1,
        )
        await backend.pin_start(started, "execute", fencing_token=1)
        backend.seal_observers()
        factory = _LeaseBoundSaverFactory(
            backend,
            revision=revision,
            root_input_digest=INPUT_DIGEST,
            build_artifact=lambda saver: _multi_artifact(
                _idle_graph(saver), _idle_graph(saver), revision=revision
            ),
        )
        application = AssuranceApplication(
            lease=backend.lease,
            owner_id="runner-a",
            start_pins=backend,
        )
        pinned = await application.start(
            invocation_id="inv-1",
            entrypoint="execute",
            graph_input={"change_id": "chg-1"},
            execution_factory=factory,
        )
        assert pinned.entrypoint == "execute"
        assert await backend.read_entrypoint("inv-1") == "execute"
        stored = await backend.journal.read_invocation_started("inv-1")
        assert stored is not None
        assert stored.product_lock_digest == started.product_lock_digest
        assert stored.root_input_digest == started.root_input_digest
        assert stored.graph_revision == started.graph_revision

        with pytest.raises(CheckpointIntegrityError):
            await application.start(
                invocation_id="inv-1",
                entrypoint="execute",
                graph_input={"change_id": "chg-other"},
                execution_factory=factory,
            )
        other_factory = _LeaseBoundSaverFactory(
            backend,
            revision=_multi_revision(lock="e" * 64),
            root_input_digest=INPUT_DIGEST,
            build_artifact=lambda saver: _multi_artifact(
                _idle_graph(saver),
                _idle_graph(saver),
                revision=_multi_revision(lock="e" * 64),
            ),
        )
        with pytest.raises(CheckpointIntegrityError):
            await application.start(
                invocation_id="inv-1",
                entrypoint="execute",
                graph_input={"change_id": "chg-1"},
                execution_factory=other_factory,
            )
        with pytest.raises(CheckpointIntegrityError):
            await application.start(
                invocation_id="inv-1",
                entrypoint="other",
                graph_input={"change_id": "chg-1"},
                execution_factory=factory,
            )
        assert await backend.read_entrypoint("inv-1") == "execute"
        stored = await backend.journal.read_invocation_started("inv-1")
        assert stored is not None
        assert stored.product_lock_digest == started.product_lock_digest
        assert stored.root_input_digest == started.root_input_digest
        assert stored.graph_revision == started.graph_revision
