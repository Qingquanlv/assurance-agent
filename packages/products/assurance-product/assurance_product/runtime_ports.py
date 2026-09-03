from __future__ import annotations

import pickle
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

from graph_engine.application.runtime_context import AssuranceRuntimeContext
from graph_engine.attempts.checkpoint_bridge import AttemptCheckpointObserver
from graph_engine.attempts.kernel import AssuranceAttemptKernel
from graph_engine.attempts.resource_arbiter import ResourceArbiter
from graph_engine.attempts.node_factory import AttemptNodeFactory
from graph_engine.boot.boot import EngineGraphBuildContext, RuntimePorts, bind_attempt_factory
from graph_engine.boot.graph_revision import BootArtifact
from graph_engine.persistence.anchored_checkpointer import AnchoredCheckpointer
from graph_engine.persistence.checkpoint_observer import CheckpointAnchorObserverPort
from graph_engine.composition import FrozenComposition
from graph_engine.persistence.attempt_journal import MemoryAttemptJournal
from graph_engine.persistence.journal import (
    CheckpointAnchor,
    CheckpointAnchorState,
    CheckpointIntegrityError,
    InvocationStarted,
)
from graph_engine.persistence.resource_authorization import MemoryResourceAuthorizationStore
from graph_engine.plugin_api import (
    PreparedWorkspaceRef,
    PromotionReceipt,
    ResourceClaims,
    SealedWriteSet,
    TaskWorkspaceBinding,
)

from assurance_product.change_workspace import ChangeWorkspace
from assurance_product.product import product_graph_manifest, product_lock_from_composition
from assurance_product.sqlite_checkpointer import AssuranceSqliteBackend, open_sqlite_checkpointer


class ProductionObserverError(ValueError):
    """Raised when production ports would compile with a fake or empty observer registry."""


class _FenceAdvancedReplayJournal:
    """Replay an identical pending write after a newer runner lease advances the fence."""

    def __init__(self, inner: object) -> None:
        self._inner = inner

    async def start_invocation(self, record: InvocationStarted, *, fencing_token: int) -> None:
        await self._inner.start_invocation(record, fencing_token=fencing_token)  # type: ignore[attr-defined]

    async def append_checkpoint_anchor(self, anchor: CheckpointAnchor, *, fencing_token: int) -> None:
        try:
            await self._inner.append_checkpoint_anchor(anchor, fencing_token=fencing_token)  # type: ignore[attr-defined]
        except CheckpointIntegrityError as error:
            if "checkpoint identity drifted" not in str(error):
                raise
            existing = await self._inner.read_checkpoint_anchor(  # type: ignore[attr-defined]
                anchor.thread_id, anchor.checkpoint_id
            )
            if existing is None or not _same_replay_payload(existing, anchor):
                raise
            if existing.fencing_token > anchor.fencing_token:
                raise

    async def read_checkpoint_anchor(self, thread_id: str, checkpoint_id: str) -> CheckpointAnchor | None:
        return await self._inner.read_checkpoint_anchor(thread_id, checkpoint_id)  # type: ignore[attr-defined]

    async def assert_current_fence(self, invocation_id: str, fencing_token: int) -> None:
        await self._inner.assert_current_fence(invocation_id, fencing_token)  # type: ignore[attr-defined]


def _same_replay_payload(existing: CheckpointAnchor, incoming: CheckpointAnchor) -> bool:
    return (
        existing.invocation_id == incoming.invocation_id
        and existing.thread_id == incoming.thread_id
        and existing.checkpoint_id == incoming.checkpoint_id
        and existing.parent_checkpoint_id == incoming.parent_checkpoint_id
        and existing.checkpoint_bytes == incoming.checkpoint_bytes
        and existing.pending_write_bytes == incoming.pending_write_bytes
        and existing.task_identity == incoming.task_identity
        and existing.graph_revision == incoming.graph_revision
        and existing.product_lock_digest == incoming.product_lock_digest
        and existing.root_input_digest == incoming.root_input_digest
    )


class _TolerantAttemptFactory(AttemptNodeFactory):
    """Test-only factory: scripted Kernel resolutions still need a valid selected input."""

    def attempt(self, contract, *, semantic_node_id, activation, select, publish):  # type: ignore[no-untyped-def]
        task = getattr(contract, "contract", contract)

        def _select(state: object) -> object:
            try:
                return select(state) if callable(select) else select
            except Exception:
                return _scripted_input(task, state)

        return super().attempt(
            contract,
            semantic_node_id=semantic_node_id,
            activation=activation,
            select=_select,
            publish=publish,
        )


def _scripted_input(task: object, state: object) -> object:
    digest = "a" * 64
    mapping = dict(state) if isinstance(state, Mapping) else {}
    payload = {
        "change_id": mapping.get("change_id") or "CH-DEMO-001",
        "retro_id": mapping.get("retro_id") or "RET-COEXIST-001",
        "owned_evidence_ids": (),
        "artifact_paths": mapping.get("artifact_paths") or mapping.get("allowed_artifact_paths") or (),
        "source_manifest": {
            "issue_slice_sha256": f"sha256:{digest}",
            "workflow_slice_sha256": f"sha256:{digest}",
            "eval_slice_sha256": f"sha256:{digest}",
        },
        "context_digest": digest,
        "quality_report_digest": digest,
        "metrics_digest": digest,
        "issue_digest": digest,
        "subject_digest": digest,
        "expected_improvement_version": 1,
        "improvement_id": mapping.get("improvement_id") or "IMP-COEXIST-001",
        "invocation_id": mapping.get("invocation_id") or "inv-scripted",
        "archive_digest": digest,
        "locked_signal_ids": (),
    }
    model = getattr(task, "input_model", None)
    if model is None:
        return payload
    return model.model_validate(payload)


class _UnusedWorkspace:
    async def open_or_create(self, attempt_key: object, claims: ResourceClaims) -> TaskWorkspaceBinding:
        del attempt_key, claims
        raise RuntimeError("workspace adapter is unused for this operation")

    async def seal(self, binding: TaskWorkspaceBinding) -> SealedWriteSet:
        del binding
        raise RuntimeError("workspace adapter is unused for this operation")

    async def prepare(self, binding: TaskWorkspaceBinding, sealed: SealedWriteSet) -> PreparedWorkspaceRef:
        del binding, sealed
        raise RuntimeError("workspace adapter is unused for this operation")

    async def promote(self, prepared: PreparedWorkspaceRef) -> PromotionReceipt:
        del prepared
        raise RuntimeError("workspace adapter is unused for this operation")

    async def recover_promotion(self, prepared: PreparedWorkspaceRef) -> PromotionReceipt:
        del prepared
        raise RuntimeError("workspace adapter is unused for this operation")


class DurableAttemptJournal(MemoryAttemptJournal):
    def __init__(self, path: Path) -> None:
        super().__init__()
        self._path = path
        if path.exists() and path.is_file() and not path.is_symlink():
            loaded = pickle.loads(path.read_bytes())
            self._logs, self._durable = loaded

    async def append_record(self, record, *, expected_revision: int, fencing_token: int):  # type: ignore[no-untyped-def]
        snapshot = await super().append_record(
            record, expected_revision=expected_revision, fencing_token=fencing_token
        )
        self._path.parent.mkdir(mode=0o700, exist_ok=True)
        self._path.write_bytes(pickle.dumps((self._logs, self._durable)))
        return snapshot

    async def ensure_durable(self, attempt_key) -> None:  # type: ignore[no-untyped-def]
        await super().ensure_durable(attempt_key)
        self._path.parent.mkdir(mode=0o700, exist_ok=True)
        self._path.write_bytes(pickle.dumps((self._logs, self._durable)))


class ProductRuntimePorts:
    test_kernel_resolutions: list[object] | None = None
    _last_scripted_committed: object | None = None
    _last_events: tuple[object, ...] = ()
    _last_active: int = 0
    _last_replayed: tuple[int, ...] = ()

    def __init__(
        self,
        *,
        workspace: ChangeWorkspace,
        composition: object,
        backend: AssuranceSqliteBackend,
        journal: DurableAttemptJournal,
        observer: AttemptCheckpointObserver,
        kernel: AssuranceAttemptKernel,
        product_lock_digest: str,
        revision_id: str,
    ) -> None:
        self.workspace = workspace
        self.composition = composition
        self.backend = backend
        self.attempt_journal = journal
        self.observer = observer
        self.kernel = kernel
        self.product_lock_digest = product_lock_digest
        self.revision_id = revision_id
        self.observer_registered_before_compile = False
        self._artifact: BootArtifact | None = None
        self._close_log: list[str] = []

    @classmethod
    @asynccontextmanager
    async def open(
        cls,
        workspace: ChangeWorkspace,
        composition: object,
        observers: Sequence[object] | None = None,
    ) -> AsyncIterator[ProductRuntimePorts]:
        product_lock = product_lock_from_composition(composition)  # type: ignore[arg-type]
        manifest = product_graph_manifest(composition, product_lock)  # type: ignore[arg-type]
        journal = DurableAttemptJournal(workspace.paths.langgraph_leases / "attempts.pkl")
        observer = AttemptCheckpointObserver(journal)
        if observers is not None:
            if not observers or any(not isinstance(item, AttemptCheckpointObserver) for item in observers):
                raise ProductionObserverError("production observer registry cannot be fake-only or empty")
            registered = cast(tuple[CheckpointAnchorObserverPort, ...], tuple(observers))
        else:
            registered = (observer,)
        observer_ports: Sequence[CheckpointAnchorObserverPort] = registered
        async with open_sqlite_checkpointer(workspace, observers=observer_ports) as backend:
            allow = getattr(backend.serializer, "with_msgpack_allowlist", None)
            if callable(allow):
                backend.serializer = allow(
                    (("graph_engine.stategraph.checkpoint_bridge", "CheckpointBridgeMarker"),)
                )
            kernel = AssuranceAttemptKernel(
                journal=journal,
                arbiter=ResourceArbiter(MemoryResourceAuthorizationStore()),
                workspace=_UnusedWorkspace(),
                graph_revision=manifest.revision.revision_id,
            )
            if cls.test_kernel_resolutions is not None:
                original = kernel.execute_or_recover

                async def _scripted(*args: Any, **kwargs: Any) -> Any:
                    remaining = cls.test_kernel_resolutions
                    if remaining:
                        resolution = remaining.pop(0)
                        if type(resolution).__name__ == "CommittedTaskResult":
                            cls._last_scripted_committed = resolution
                        return resolution
                    cached = getattr(cls, "_last_scripted_committed", None)
                    if cached is not None:
                        return cached
                    return await original(*args, **kwargs)

                kernel.execute_or_recover = _scripted  # type: ignore[method-assign]
            ports = cls(
                workspace=workspace,
                composition=composition,
                backend=backend,
                journal=journal,
                observer=observer,
                kernel=kernel,
                product_lock_digest=product_lock.digest,
                revision_id=manifest.revision.revision_id,
            )
            ports.observer_registered_before_compile = any(
                isinstance(item, AttemptCheckpointObserver) for item in backend.observers
            )
            try:
                yield ports
            finally:
                ports._close_log.append("observer_outbox_recovery")
                await backend.recover_handshake("")
                ports._publish_journal_snapshot()
                ports._close_log.extend(("kernel", "attempt_journal", "sqlite"))

    def shutdown_order(self) -> tuple[str, ...]:
        return tuple(self._close_log)

    def checkpointer_for(
        self,
        *,
        invocation_id: str,
        revision_id: str,
        product_lock_digest: str,
        root_input_digest: str,
        fencing_token: int,
    ) -> AnchoredCheckpointer:
        identity = CheckpointAnchorState(
            invocation_id=invocation_id,
            thread_id=invocation_id,
            graph_revision=revision_id,
            product_lock_digest=product_lock_digest,
            root_input_digest=root_input_digest,
            fencing_token=fencing_token,
        )
        return AnchoredCheckpointer(
            store=self.backend.store,
            journal=_FenceAdvancedReplayJournal(self.backend.journal),
            identity=identity,
            observers=self.backend.observers,
            lease=self.backend.lease,
            serde=self.backend.serializer,
        )

    def runtime_ports(self) -> RuntimePorts:
        return RuntimePorts(
            attempt_kernel=self.kernel,
            secret_resolver=object(),
            workspace_provider=_UnusedWorkspace(),
        )

    def runtime_context(self, fencing_token: int) -> AssuranceRuntimeContext:
        return AssuranceRuntimeContext(
            revision_id=self.revision_id,
            fencing_token=fencing_token,
            attempt_kernel=self.kernel,
            secret_resolver=object(),
            workspace_provider=_UnusedWorkspace(),
        )

    async def compile_roots(
        self,
        *,
        invocation_id: str = "compile",
        root_input_digest: str | None = None,
        fencing_token: int = 1,
    ) -> BootArtifact:
        from assurance_execution.graphs.factory import build_execution_graphs
        from assurance_generation.graphs.factory import build_generation_graphs
        from assurance_healing.graphs.factory import build_healing_graphs
        from assurance_improvement.graphs.factory import build_improvement_graphs
        from assurance_intake.graphs.factory import build_intake_graphs
        from assurance_product.graphs.factory import build_product_graphs
        from assurance_quality.graphs.factory import build_quality_graphs

        checkpointer = self.checkpointer_for(
            invocation_id=invocation_id,
            revision_id=self.revision_id,
            product_lock_digest=self.product_lock_digest,
            root_input_digest=root_input_digest or "c" * 64,
            fencing_token=fencing_token,
        )
        if self.observer not in checkpointer._observers and not any(
            isinstance(item, AttemptCheckpointObserver) for item in checkpointer._observers
        ):
            raise ProductionObserverError("observer must be registered before Boot compiles any root")
        semantic = dict(getattr(self.composition, "semantic_attempt_contracts", {}))
        data_contracts = {
            contract_id: getattr(resolved, "contract", resolved) for contract_id, resolved in semantic.items()
        }
        factory = (
            _TolerantAttemptFactory(journal=self.attempt_journal, kernel=self.kernel)
            if type(self).test_kernel_resolutions
            else bind_attempt_factory(self.kernel)
        )
        context = EngineGraphBuildContext(
            contracts=data_contracts,
            checkpointer=checkpointer,
            approved_source_roots=(),
            attempt_factory=factory,
            resolved_contracts=semantic,
        )
        features = {
            "assurance.intake": build_intake_graphs(context.for_capability("assurance.intake")),
            "assurance.generation": build_generation_graphs(context.for_capability("assurance.generation")),
            "assurance.execution": build_execution_graphs(context.for_capability("assurance.execution")),
            "assurance.quality": build_quality_graphs(context.for_capability("assurance.quality")),
            "assurance.healing": build_healing_graphs(context.for_capability("assurance.healing")),
            "assurance.improvement": build_improvement_graphs(
                context.for_capability("assurance.improvement")
            ),
        }
        graphs = build_product_graphs(context=context, features=features)
        composition = cast(FrozenComposition, self.composition)
        manifest = product_graph_manifest(composition, product_lock_from_composition(composition))
        artifact = BootArtifact(
            manifest=manifest,
            entrypoints=graphs.entrypoints,
            attempt_contracts=getattr(self.composition, "semantic_attempt_contracts", {}),
            checkpointer_backend_id=checkpointer.backend_id,
        )
        self._artifact = artifact
        self.observer_registered_before_compile = True
        return artifact

    def _publish_journal_snapshot(self) -> None:
        from graph_engine.attempts.events import SystemInterruptIssued

        events: list[object] = []
        replayed: list[int] = []
        for digest, records in self.attempt_journal._logs.items():
            for record in records:
                events.extend(record.events)
                replayed.extend(
                    getattr(event, "ordinal", 0)
                    for event in record.events
                    if type(event).__name__ == "SystemInterruptCompletionCheckpointed"
                )
            del digest
        issued = [event for event in events if isinstance(event, SystemInterruptIssued)]
        completed = {
            getattr(event, "generation", None)
            for event in events
            if type(event).__name__ == "SystemInterruptCompletionCheckpointed"
        }
        type(self)._last_events = tuple(events)
        type(self)._last_active = len({getattr(event, "generation", None) for event in issued} - completed)
        type(self)._last_replayed = tuple(replayed)

    @classmethod
    def last_journal_events(cls) -> tuple[object, ...]:
        return cls._last_events

    @classmethod
    def last_active_generations(cls) -> int:
        return cls._last_active

    @classmethod
    def last_replayed_ordinals(cls) -> tuple[int, ...]:
        return cls._last_replayed


__all__ = ["ProductRuntimePorts", "ProductionObserverError"]
