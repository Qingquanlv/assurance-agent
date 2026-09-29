from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, cast

from graph_engine.application.application import InvocationBoundExecution
from graph_engine.application.runtime_context import AssuranceRuntimeContext
from graph_engine.attempts.activity import JournalBackedTaskActivityPort
from graph_engine.attempts.checkpoint_bridge import AttemptCheckpointObserver
from graph_engine.attempts.host_protocol import (
    TaskHostCancelCall,
    TaskHostExecuteCall,
    TaskHostReconcileCall,
)
from graph_engine.attempts.host_receipts import TerminalReceiptStore
from graph_engine.attempts.kernel import AssuranceAttemptKernel
from graph_engine.attempts.keys import AttemptKey
from graph_engine.attempts.production_host import (
    create_production_task_execution_host,
    invocation_activity_receipts_root,
)
from graph_engine.attempts.resource_arbiter import ResourceArbiter
from graph_engine.attempts.secret_sources import (
    InvocationRuntimeAuthorization,
    authorize_binding_secret_handles,
    empty_runtime_authorization,
    resolve_secret_source,
)
from graph_engine.attempts.workspace import TaskWorkspaceProvider, TaskWorkspaceStore
from graph_engine.attempts.contracts import (
    AttemptExecutor,
    ResolvedAttemptContract,
    resolve_contract,
)
from graph_engine.boot.boot import EngineGraphBuildContext, RuntimePorts, bind_attempt_factory
from graph_engine.boot.graph_revision import BootArtifact
from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.composition import FrozenComposition
from graph_engine.persistence.anchored_checkpointer import AnchoredCheckpointer
from graph_engine.persistence.checkpoint_observer import CheckpointAnchorObserverPort
from graph_engine.persistence.journal import (
    CheckpointAnchor,
    CheckpointAnchorState,
    CheckpointIntegrityError,
    InvocationStarted,
)
from graph_engine.persistence.runner_lease import RunnerLease

from assurance_product.change_workspace import ChangeWorkspace
from assurance_product.product import product_graph_manifest, product_lock_from_composition
from assurance_product.runtime_bindings import runtime_bindings_from_composition
from assurance_product.sqlite_attempt_store import SqliteAttemptJournal
from assurance_product.sqlite_checkpointer import AssuranceSqliteBackend, open_sqlite_checkpointer
from assurance_product.sqlite_effect_state import SQLiteEffectState
from assurance_product.sqlite_resource_authorization import SqliteResourceAuthorizationStore

_logger = logging.getLogger(__name__)


class ProductionObserverError(ValueError):
    """Raised when production ports would compile with a fake or empty observer registry."""


class AuthorizedSecretResolver:
    def __init__(self, authorization: InvocationRuntimeAuthorization) -> None:
        self._authorization = authorization

    def resolve(self, handle: str) -> bytes:
        for binding in self._authorization.secret_sources:
            if binding.handle == handle:
                return resolve_secret_source(binding)
        raise ValueError(f"secret handle is not authorized: {handle}")


@dataclass(frozen=True, slots=True)
class NetworkPolicy:
    allow_opencode: bool


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


def _invocation_id(invocation: object | None) -> str:
    if invocation is None:
        return "ports"
    if isinstance(invocation, str):
        if not invocation:
            raise ValueError("invocation id must be nonempty")
        return invocation
    value = getattr(invocation, "invocation_id", None)
    if not isinstance(value, str) or not value:
        raise ValueError("invocation id must be nonempty")
    return value


class _ProductExecutionFactory:
    def __init__(
        self,
        ports: ProductRuntimePorts,
        *,
        invocation_id: str,
        entrypoint: str,
        root_input_digest: str,
    ) -> None:
        self._ports = ports
        self._invocation_id = invocation_id
        self._entrypoint = entrypoint
        self._root_input_digest = root_input_digest
        self._bound = False

    def bind(self, runner_lease: RunnerLease) -> InvocationBoundExecution:
        if not isinstance(runner_lease, RunnerLease):
            raise ValueError("fencing token")
        if runner_lease.fencing_token < 1:
            raise ValueError("fencing token")
        if self._bound:
            raise ValueError("execution factory already bound")
        self._bound = True
        artifact = self._ports._compile_bound(
            invocation_id=self._invocation_id,
            root_input_digest=self._root_input_digest,
            fencing_token=runner_lease.fencing_token,
        )
        context = AssuranceRuntimeContext(
            revision_id=self._ports.revision_id,
            fencing_token=runner_lease.fencing_token,
            attempt_kernel=self._ports.kernel,
            secret_resolver=self._ports.secret_resolver,
            workspace_provider=self._ports.workspace_provider,
        )

        async def recover_outbox() -> None:
            await self._ports.backend.recover_handshake(self._invocation_id)

        return InvocationBoundExecution(
            artifact=artifact,
            runtime_context=context,
            recover_outbox=recover_outbox,
        )


class ProductRuntimePorts:
    _last_events: tuple[object, ...] = ()
    _last_active: int = 0
    _last_replayed: tuple[int, ...] = ()

    def __init__(
        self,
        *,
        workspace: ChangeWorkspace,
        composition: object,
        backend: AssuranceSqliteBackend,
        journal: SqliteAttemptJournal,
        observer: AttemptCheckpointObserver,
        kernel: AssuranceAttemptKernel,
        product_lock_digest: str,
        revision_id: str,
        secret_resolver: AuthorizedSecretResolver,
        workspace_provider: TaskWorkspaceProvider,
        effect_state: SQLiteEffectState,
        host: object,
        authorization: InvocationRuntimeAuthorization,
        reachable_contract_ids: tuple[str, ...],
        invocation_id: str,
        network: NetworkPolicy,
    ) -> None:
        self.workspace = workspace
        self.composition = composition
        self.backend = backend
        self.attempt_journal = journal
        self.observer = observer
        self.kernel = kernel
        self.product_lock_digest = product_lock_digest
        self.revision_id = revision_id
        self.secret_resolver = secret_resolver
        self.workspace_provider = workspace_provider
        self.effect_state = effect_state
        self.host = host
        self.authorization = authorization
        self.reachable_contract_ids = reachable_contract_ids
        self.invocation_id = invocation_id
        self.network = network
        self.observer_registered_before_compile = False
        self._artifact: BootArtifact | None = None
        self._close_log: list[str] = []

    @classmethod
    @asynccontextmanager
    async def open(
        cls,
        workspace: ChangeWorkspace,
        composition: object,
        invocation: object | None = None,
        authorization: InvocationRuntimeAuthorization | None = None,
        reachable_contract_ids: Sequence[str] | None = None,
        observers: Sequence[object] | None = None,
        pause_requested: Callable[[], bool] | None = None,
    ) -> AsyncIterator[ProductRuntimePorts]:
        typed_composition = cast(FrozenComposition, composition)
        product_lock = product_lock_from_composition(typed_composition)
        manifest = product_graph_manifest(typed_composition, product_lock)
        invocation_id = _invocation_id(invocation)
        auth = authorization if authorization is not None else empty_runtime_authorization()
        reachable = tuple(reachable_contract_ids or ())
        async with open_sqlite_checkpointer(workspace) as backend:
            journal = SqliteAttemptJournal(backend)
            observer = AttemptCheckpointObserver(journal)
            if observers is not None:
                if not observers or any(
                    not isinstance(item, AttemptCheckpointObserver) for item in observers
                ):
                    raise ProductionObserverError("production observer registry cannot be fake-only or empty")
                registered = cast(tuple[CheckpointAnchorObserverPort, ...], tuple(observers))
            else:
                registered = (observer,)
            backend.install_observers(registered)
            backend.seal_observers()
            allow = getattr(backend.serializer, "with_msgpack_allowlist", None)
            if callable(allow):
                backend.serializer = allow(
                    (("graph_engine.stategraph.checkpoint_bridge", "CheckpointBridgeMarker"),)
                )
            binding = workspace.runtime_binding()
            task_store = TaskWorkspaceStore(
                binding.project_root,
                binding.attempts_root,
                binding.receipts_root,
            )
            run_dir = workspace.paths.project_root / ".aa" / "runs" / invocation_id
            if run_dir.is_dir():
                from assurance_product.run_history import RunOutputWorkspaceProvider

                workspace_provider = RunOutputWorkspaceProvider(task_store, run_dir)
            else:
                workspace_provider = TaskWorkspaceProvider(task_store)
            receipts_root = invocation_activity_receipts_root(workspace.paths.qa_root, invocation_id)
            receipts_root.parent.mkdir(parents=True, exist_ok=True)
            receipts = TerminalReceiptStore.open_or_create(receipts_root)
            secret_resolver = AuthorizedSecretResolver(auth)
            effect_state = SQLiteEffectState(backend)
            loop = asyncio.get_running_loop()

            async def assert_live_fence() -> None:
                current = backend.lease.current(invocation_id)  # type: ignore[attr-defined]
                await backend.lease.assert_current(invocation_id, current.fencing_token)

            def activity_factory(
                call: TaskHostExecuteCall | TaskHostReconcileCall | TaskHostCancelCall,
                *,
                remaining_deadline: float,
            ) -> JournalBackedTaskActivityPort:
                return JournalBackedTaskActivityPort(
                    journal=journal,
                    attempt_key=AttemptKey(digest=call.identity.attempt_key_digest),
                    identity=call.activity_rpc,
                    workspace_identity=call.attempt_root.workspace_identity,
                    assert_live_fence=assert_live_fence,
                    owner_loop=loop,
                    remaining_deadline=remaining_deadline,
                    expected_request_digest=canonical_digest(
                        cast(JSONValue, call.request.model_dump(mode="json"))
                    ),
                    expected_product_lock_digest=call.request.invocation.lock_digest,
                    expected_handler_id=call.request.capability_id,
                )

            host = create_production_task_execution_host(
                authorization=auth,
                handlers=typed_composition.registries.capabilities.task_handlers,
                store=task_store,
                receipts=receipts,
                activity_factory=activity_factory,
                invocation_root=workspace.paths.qa_root,
            )
            network = _preflight_selected_root(typed_composition, auth, reachable)
            expected_allow = any(".agent." in contract_id for contract_id in reachable)
            if network.allow_opencode != expected_allow:
                raise ValueError("network policy does not match selected root")
            kernel = AssuranceAttemptKernel(
                journal=journal,
                arbiter=ResourceArbiter(SqliteResourceAuthorizationStore(backend)),
                workspace=workspace_provider,
                graph_revision=manifest.revision.revision_id,
                validators=typed_composition.registries.capabilities.commit_validators,
                effects=typed_composition.registries.effects,
                schemas=typed_composition.registries.schemas,
                effect_state=effect_state,
                pause_requested=pause_requested,
            )
            ports = cls(
                workspace=workspace,
                composition=composition,
                backend=backend,
                journal=journal,
                observer=observer,
                kernel=kernel,
                product_lock_digest=product_lock.digest,
                revision_id=manifest.revision.revision_id,
                secret_resolver=secret_resolver,
                workspace_provider=workspace_provider,
                effect_state=effect_state,
                host=host,
                authorization=auth,
                reachable_contract_ids=reachable,
                invocation_id=invocation_id,
                network=network,
            )
            ports.observer_registered_before_compile = any(
                isinstance(item, AttemptCheckpointObserver) for item in backend.observers
            )
            try:
                yield ports
            finally:
                ports._close_log.append("observer_outbox_recovery")
                try:
                    await backend.recover_handshake("")
                    await ports._publish_journal_snapshot()
                finally:
                    task_store.close()
                    ports._close_log.extend(("kernel", "attempt_journal", "sqlite"))

    def shutdown_order(self) -> tuple[str, ...]:
        return tuple(self._close_log)

    def execution_factory(
        self,
        *,
        invocation_id: str,
        entrypoint: str,
        root_input_digest: str,
    ) -> _ProductExecutionFactory:
        return _ProductExecutionFactory(
            self,
            invocation_id=invocation_id,
            entrypoint=entrypoint,
            root_input_digest=root_input_digest,
        )

    def checkpointer_for(
        self,
        *,
        invocation_id: str,
        revision_id: str,
        product_lock_digest: str,
        root_input_digest: str,
        fencing_token: int,
    ) -> AnchoredCheckpointer:
        if fencing_token < 1:
            raise ValueError("fencing token")
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
            secret_resolver=self.secret_resolver,
            workspace_provider=self.workspace_provider,
        )

    def runtime_context(self, fencing_token: int) -> AssuranceRuntimeContext:
        if fencing_token < 1:
            raise ValueError("fencing token")
        return AssuranceRuntimeContext(
            revision_id=self.revision_id,
            fencing_token=fencing_token,
            attempt_kernel=self.kernel,
            secret_resolver=self.secret_resolver,
            workspace_provider=self.workspace_provider,
        )

    def _compile_bound(
        self,
        *,
        invocation_id: str,
        root_input_digest: str,
        fencing_token: int,
    ) -> BootArtifact:
        from assurance_execution.graphs.factory import build_execution_graphs
        from assurance_generation.graphs.factory import build_generation_graphs
        from assurance_healing.graphs.factory import build_healing_graphs
        from assurance_improvement.graphs.factory import build_improvement_graphs
        from assurance_intake.graphs.factory import build_intake_graphs
        from assurance_product.graphs.factory import build_product_graphs
        from assurance_product.retro_evidence import snapshot_runtime_evidence
        from assurance_quality.graphs.factory import build_quality_graphs

        if fencing_token < 1:
            raise ValueError("fencing token")
        checkpointer = self.checkpointer_for(
            invocation_id=invocation_id,
            revision_id=self.revision_id,
            product_lock_digest=self.product_lock_digest,
            root_input_digest=root_input_digest,
            fencing_token=fencing_token,
        )
        if self.observer not in checkpointer._observers and not any(
            isinstance(item, AttemptCheckpointObserver) for item in checkpointer._observers
        ):
            raise ProductionObserverError("observer must be registered before Boot compiles any root")
        semantic = {
            contract_id: _bind_executor_host(
                cast(ResolvedAttemptContract[Any, Any], resolved),
                host=self.host,
                graph_revision=self.revision_id,
                product_lock_digest=self.product_lock_digest,
            )
            for contract_id, resolved in dict(
                getattr(self.composition, "semantic_attempt_contracts", {})
            ).items()
        }
        data_contracts = {contract_id: resolved.contract for contract_id, resolved in semantic.items()}
        factory = bind_attempt_factory(self.kernel)
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

        async def runtime_snapshot():
            return await snapshot_runtime_evidence(
                self.workspace,
                self.attempt_journal.read_records,
                invocation_id=invocation_id,
            )

        graphs = build_product_graphs(
            context=context,
            features=features,
            runtime_snapshot=runtime_snapshot,
        )
        composition = cast(FrozenComposition, self.composition)
        manifest = product_graph_manifest(composition, product_lock_from_composition(composition))
        artifact = BootArtifact(
            manifest=manifest,
            entrypoints=graphs.entrypoints,
            attempt_contracts=semantic,
            checkpointer_backend_id=checkpointer.backend_id,
        )
        self._artifact = artifact
        self.observer_registered_before_compile = True
        return artifact

    async def read_only_execution(
        self,
        *,
        invocation_id: str,
        root_input_digest: str,
    ) -> InvocationBoundExecution:
        from graph_engine.application.application import _read_only_artifact

        started = await self.backend.journal.read_invocation_started(invocation_id)
        if started is None:
            raise ValueError("invocation has not started")
        artifact = self._compile_bound(
            invocation_id=invocation_id,
            root_input_digest=root_input_digest or started.root_input_digest,
            fencing_token=started.fencing_token,
        )
        return InvocationBoundExecution(
            artifact=_read_only_artifact(artifact),
            runtime_context=self.runtime_context(started.fencing_token),
        )

    async def _publish_journal_snapshot(self) -> None:
        from graph_engine.attempts.events import SystemInterruptIssued

        events: list[object] = []
        replayed: list[int] = []
        records = await self.attempt_journal.read_records()
        run_dir = self.workspace.paths.project_root / ".aa" / "runs" / self.invocation_id
        if run_dir.is_dir():
            from assurance_product.operator_views import write_attempt_projection

            try:
                write_attempt_projection(run_dir, records, self.invocation_id)
            except (OSError, ValueError) as error:
                _logger.warning("run_attempt_projection_failed: %s", error)
        for record in records:
            events.extend(record.events)
            replayed.extend(
                getattr(event, "ordinal", 0)
                for event in record.events
                if type(event).__name__ == "SystemInterruptCompletionCheckpointed"
            )
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


def _bind_executor_host(
    resolved: ResolvedAttemptContract[Any, Any],
    *,
    host: object,
    graph_revision: str,
    product_lock_digest: str,
) -> ResolvedAttemptContract[Any, Any]:
    bind = getattr(resolved.executor, "with_host", None)
    if not callable(bind):
        return resolved
    return resolve_contract(
        resolved.contract,
        executor=cast(
            AttemptExecutor[Any, Any],
            bind(
                host,
                graph_revision=graph_revision,
                product_lock_digest=product_lock_digest,
            ),
        ),
        validation_context=resolved.validation_context,
    )


def _preflight_selected_root(
    composition: FrozenComposition,
    authorization: InvocationRuntimeAuthorization,
    reachable: Sequence[str],
) -> NetworkPolicy:
    semantic = getattr(composition, "semantic_attempt_contracts", {})
    if len(semantic) != 44:
        raise ValueError("composition must resolve all 44 semantic contracts")
    missing_reachable = tuple(contract_id for contract_id in reachable if contract_id not in semantic)
    if missing_reachable:
        raise ValueError(f"missing required port for contract {missing_reachable[0]}")
    agents = tuple(contract_id for contract_id in reachable if ".agent." in contract_id)
    policy = NetworkPolicy(allow_opencode=bool(agents))
    if not agents:
        return policy
    bindings = runtime_bindings_from_composition(composition)
    handles: list[str] = []
    for contract_id in agents:
        binding = bindings.get(contract_id)
        if binding is None:
            raise ValueError(f"missing required port for contract {contract_id}")
        handles.extend(binding.secret_handles)
    if handles:
        authorize_binding_secret_handles(tuple(sorted(set(handles))), authorization)
    if not authorization.secret_sources:
        raise ValueError("missing required port: OpenCode credentials")
    return policy


__all__ = ["AuthorizedSecretResolver", "NetworkPolicy", "ProductRuntimePorts", "ProductionObserverError"]
