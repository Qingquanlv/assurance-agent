from __future__ import annotations

import asyncio
import fcntl
import os
import stat
import sys
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from typing import Literal, Self, cast

from pydantic import BaseModel, ConfigDict, model_validator

from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.composition import FrozenComposition
from graph_engine.errors import GraphEngineError
from graph_engine.plugin_api import (
    TaskActivityCancelResult,
    TaskActivityReconcileResult,
    TaskOutcome,
)
from graph_engine.runtime.checkpoint import load_checkpoint_at, write_checkpoint_at
from graph_engine.runtime.effects import (
    EffectExecutor,
    EffectPublicationIndeterminate,
    EffectStateError,
    needs_settlement,
)
from graph_engine.runtime.events import (
    EventEnvelope,
    GraphStarted,
    InterruptResumed,
    InvocationStarted,
    NodeCompleted,
    RuntimeEvent,
    TokenOffered,
)
from graph_engine.runtime.frozen_json import FrozenJSONValue, freeze_json, thaw_json
from graph_engine.runtime.invocation_lock import (
    InvocationStartIntent,
    InvocationDrift,
    _rename_no_replace_at,
    authenticate_invocation_lock,
    authenticate_invocation_start_intent,
    install_invocation_lock_at,
    install_invocation_start_intent_at,
)
from graph_engine.runtime.ledger import (
    Ledger,
    LedgerConflictError,
    LedgerPublicationIndeterminate,
    append_validated_batch,
)
from graph_engine.runtime.models import InvocationProjection, RecoveryResult, fold_events
from graph_engine.runtime.planner import (
    PlanningError,
    _start_token_id,
    plan_next,
    plan_running_tasks,
    validate_event_history,
    validate_projection,
)
from graph_engine.runtime.host_protocol import (
    TaskExecutionHost,
    TaskHostCallIdentity,
    TaskHostCallResult,
    TaskHostCancelCall,
    TaskHostExecuteCall,
    TaskHostReconcileCall,
    TaskHostTerminalReceipt,
)
from graph_engine.runtime.host_receipts import TerminalReceiptStore
from graph_engine.runtime.scheduler import (
    Clock,
    LeaseUnavailableError,
    Scheduler,
    SystemClock,
)
from graph_engine.runtime.seed import InvocationSeed
from graph_engine.runtime.secret_sources import InvocationRuntimeAuthorization
from graph_engine.runtime.workspace import (
    FinalizationRolledBack,
    HeadPublicationIndeterminate,
    SnapshotStore,
)


class EngineError(GraphEngineError):
    """Raised when an invocation cannot safely make the requested transition."""


class EngineConflictError(EngineError):
    """Raised when another runner wins an invocation transition."""


class EnginePublicationIndeterminate(EngineError):
    """Raised when authoritative publication cannot be reconciled."""


@dataclass(frozen=True, slots=True)
class InvocationHandle:
    invocation_id: str
    invocation_root: Path
    lock_digest: str
    _entrypoint: str = field(repr=False, compare=False)
    _composition: FrozenComposition = field(repr=False, compare=False)
    _invocation_fd: int = field(repr=False, compare=False)
    _engine: Engine = field(repr=False, compare=False)
    _authorization: InvocationRuntimeAuthorization = field(repr=False, compare=False)
    _closed: bool = field(default=False, init=False, repr=False, compare=False)

    @property
    def ledger(self) -> Ledger:
        if self._closed:
            raise EngineError("invocation handle is closed")
        return Ledger.at(
            self._invocation_fd,
            "ledger",
            display_root=self.invocation_root / "ledger",
        )

    @property
    def workspace(self) -> SnapshotStore:
        if self._closed:
            raise EngineError("invocation handle is closed")
        return SnapshotStore.at(
            self._invocation_fd,
            "workspace",
            display_root=self.invocation_root / "workspace",
        )

    async def recover(self) -> RecoveryResult:
        if self._closed:
            raise EngineError("invocation handle is closed")
        return await self._engine._recover_invocation(self)

    def close(self) -> None:
        if self._closed:
            return
        descriptor = self._invocation_fd
        object.__setattr__(self, "_closed", True)
        object.__setattr__(self, "_invocation_fd", -1)
        os.close(descriptor)

    def __enter__(self) -> InvocationHandle:
        if self._closed:
            raise EngineError("invocation handle is closed")
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except BaseException:
            pass


_RESULT_CONFIG = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
_RUNNER_LOCK = ".engine-runner.lock"
_EMPTY_TREE_ID = canonical_digest([])


class RunResult(BaseModel):
    model_config = _RESULT_CONFIG

    status: Literal["succeeded", "failed", "stopped", "interrupted"]
    output: FrozenJSONValue = None
    terminal_reason: str | None = None
    actions: tuple[str, ...] = ()
    projection: InvocationProjection

    @model_validator(mode="after")
    def _validate_terminal_reason(self) -> Self:
        if self.status == "succeeded" and self.terminal_reason is not None:
            raise ValueError("successful run cannot have a terminal reason")
        return self

    @property
    def reason(self) -> str | None:
        return self.terminal_reason


class _UnavailableTaskHost:
    async def execute(self, call: TaskHostExecuteCall) -> TaskHostCallResult:
        del call
        return TaskHostCallResult(
            operation="execute",
            outcome=TaskOutcome.failed("internal", "task execution host is not configured"),
        )

    async def reconcile(self, call: TaskHostReconcileCall) -> TaskHostCallResult:
        del call
        return TaskHostCallResult(
            operation="reconcile",
            reconcile_result=TaskActivityReconcileResult(
                status="indeterminate",
                reason="task execution host is not configured",
            ),
        )

    async def cancel(self, call: TaskHostCancelCall) -> TaskHostCallResult:
        del call
        return TaskHostCallResult(
            operation="cancel",
            cancel_result=TaskActivityCancelResult(
                status="indeterminate",
                reason="task execution host is not configured",
            ),
        )

    def read_terminal_receipts(self, identity: TaskHostCallIdentity) -> tuple[TaskHostTerminalReceipt, ...]:
        del identity
        return ()


class Engine:
    def __init__(
        self,
        root: Path,
        *,
        clock: Clock | None = None,
        host: TaskExecutionHost | None = None,
    ) -> None:
        self._root = Path(root).absolute()
        self._invocations_root = self._root / "invocations"
        self._clock = clock or SystemClock()
        self._host = host
        self._invocations_fd = _open_or_create_namespace(self._root)
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        descriptor = self._invocations_fd
        self._closed = True
        self._invocations_fd = -1
        os.close(descriptor)

    def __enter__(self) -> Engine:
        if self._closed:
            raise EngineError("engine is closed")
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except BaseException:
            pass

    def start(
        self,
        composition: FrozenComposition,
        *,
        entrypoint: str,
        invocation_id: str,
        seed: InvocationSeed,
        authorization: InvocationRuntimeAuthorization,
    ) -> InvocationHandle:
        self._assert_namespace_path_current()
        self._invocation_root(invocation_id)
        if not isinstance(composition, FrozenComposition):
            raise TypeError("engine start requires a FrozenComposition")
        if not isinstance(seed, InvocationSeed):
            raise TypeError("engine start requires an InvocationSeed")
        if not isinstance(authorization, InvocationRuntimeAuthorization):
            raise TypeError("engine start requires an InvocationRuntimeAuthorization")
        if _entry_exists(self._invocations_fd, invocation_id):
            return self._continue_start(invocation_id, composition, entrypoint, seed, authorization)
        if entrypoint not in composition.workflow.entrypoints:
            raise EngineError(f"unknown entrypoint {entrypoint!r}")
        staging_name = f".{invocation_id}.invocation-init-{uuid.uuid4().hex}"
        staging_fd: int | None = None
        installed = False
        cleanup_attempted = False
        try:
            os.mkdir(staging_name, mode=0o700, dir_fd=self._invocations_fd)
            os.fsync(self._invocations_fd)
            staging_fd = _open_directory_at(
                self._invocations_fd,
                staging_name,
                "invocation initialization staging",
            )
            install_invocation_lock_at(staging_fd, composition.lock)
            install_invocation_start_intent_at(
                staging_fd,
                lock_digest=composition.lock_digest,
                entrypoint=entrypoint,
                runtime_authorization_digest=authorization.digest,
                root_input_digest=seed.root_input_digest,
                initial_tree_id=seed.workspace.tree_id,
            )
            _initialization_boundary("lock_installed")
            installed_now = self._install_invocation(staging_name, invocation_id, staging_fd)
            if not installed_now:
                cleanup_fd = staging_fd
                staging_fd = None
                cleanup_attempted = True
                _cleanup_staging(
                    None,
                    parent_fd=self._invocations_fd,
                    name=staging_name,
                    descriptor=cleanup_fd,
                )
                return self._continue_start(
                    invocation_id, composition, entrypoint, seed, authorization
                )
            installed = True
            return self._complete_start_at(
                invocation_id,
                composition,
                entrypoint,
                staging_fd,
                seed,
                authorization,
            )
        except BaseException as error:
            if staging_fd is not None and not installed:
                installed = _entry_matches_descriptor(
                    self._invocations_fd,
                    invocation_id,
                    staging_fd,
                )
            if not installed and not cleanup_attempted:
                cleanup_fd = staging_fd
                staging_fd = None
                cleanup_attempted = True
                _cleanup_staging(
                    error,
                    parent_fd=self._invocations_fd,
                    name=staging_name,
                    descriptor=cleanup_fd,
                )
            raise
        finally:
            if staging_fd is not None:
                _cleanup_runtime_resources(
                    sys.exception(),
                    store=None,
                    descriptors=((staging_fd, "invocation initialization staging descriptor"),),
                )

    def open(
        self,
        invocation_id: str,
        composition: FrozenComposition,
        *,
        authorization: InvocationRuntimeAuthorization,
    ) -> InvocationHandle:
        self._assert_namespace_path_current()
        if not isinstance(composition, FrozenComposition):
            raise TypeError("engine open requires a FrozenComposition")
        if not isinstance(authorization, InvocationRuntimeAuthorization):
            raise TypeError("engine open requires an InvocationRuntimeAuthorization")
        invocation_root = self._invocation_root(invocation_id)
        invocation_fd = self._open_invocation(invocation_id)
        claim_fd: int | None = None
        store: SnapshotStore | None = None
        try:
            intent = self._authenticate_invocation_records(
                invocation_id,
                composition,
                None,
                invocation_fd,
            )
            self._validate_authorization_against_intent(authorization, intent)
            preclaim_ledger = Ledger.at(
                invocation_fd,
                "ledger",
                display_root=invocation_root / "ledger",
            )
            preclaim_envelopes = preclaim_ledger.read_all()
            self._authenticate_bootstrap(
                invocation_id,
                composition,
                preclaim_envelopes,
                intent,
            )
            claim_fd = self._acquire_runner_claim(invocation_fd)
            intent = self._authenticate_invocation_records(
                invocation_id,
                composition,
                intent.entrypoint,
                invocation_fd,
            )
            ledger = Ledger.at(
                invocation_fd,
                "ledger",
                display_root=invocation_root / "ledger",
            )
            self._authenticate_transition_identity(
                invocation_id,
                composition,
                intent.entrypoint,
                invocation_fd,
                ledger,
            )
            envelopes = ledger.read_all()
            projection = fold_events(envelopes)
            if projection.invocation_id != invocation_id:
                raise EngineError("invocation ledger identity does not match its path")
            if projection.lock_digest != composition.lock_digest:
                raise InvocationDrift("invocation ledger bootstrap digest differs from its lock")
            self._validate_workflow(composition, projection)
            self._validate_history(composition, envelopes, projection)
            checkpoint = load_checkpoint_at(
                invocation_fd,
                "checkpoint.json",
                ledger_envelopes=envelopes,
            )
            if checkpoint is not None:
                projection = checkpoint.projection
            store = SnapshotStore.at(
                invocation_fd,
                "workspace",
                display_root=invocation_root / "workspace",
            )
            self._ensure_ledger_durable(ledger)
            _transition_boundary("head_recovery")
            self._authenticate_transition_identity(
                invocation_id,
                composition,
                intent.entrypoint,
                invocation_fd,
                ledger,
            )
            store.recover_head_transaction(projection.head_tree_id)
            self._authenticate_transition_identity(
                invocation_id,
                composition,
                intent.entrypoint,
                invocation_fd,
                ledger,
            )
            actual_tree_id = store.head_tree_id()
            expected_tree_id = projection.head_tree_id or _EMPTY_TREE_ID
            if actual_tree_id != expected_tree_id:
                raise EngineError("workspace HEAD disagrees with the authoritative ledger")
            self._authenticate_live_activity_workspaces(projection, store)
            if projection.status == "running":
                scheduler = self._scheduler(
                    invocation_id,
                    composition,
                    intent.entrypoint,
                    invocation_root,
                    invocation_fd,
                    store,
                    ledger,
                    authorization,
                )
                self._authenticate_transition_identity(
                    invocation_id,
                    composition,
                    intent.entrypoint,
                    invocation_fd,
                    ledger,
                )
                try:
                    scheduler.reclaim_expired()
                except LedgerConflictError as error:
                    raise EngineConflictError("another runner advanced the invocation") from error
                except LedgerPublicationIndeterminate as error:
                    raise EnginePublicationIndeterminate(
                        "lease reclamation publication is indeterminate"
                    ) from error
                self._authenticate_transition_identity(
                    invocation_id,
                    composition,
                    intent.entrypoint,
                    invocation_fd,
                    ledger,
                )
                envelopes = ledger.read_all()
                projection = fold_events(envelopes)
                self._validate_workflow(composition, projection)
                self._validate_history(composition, envelopes, projection)
            self._authenticate_transition_identity(
                invocation_id,
                composition,
                intent.entrypoint,
                invocation_fd,
                ledger,
            )
            self._write_checkpoint(invocation_fd, envelopes, projection)
            self._authenticate_transition_identity(
                invocation_id,
                composition,
                intent.entrypoint,
                invocation_fd,
                ledger,
            )
        except BaseException as error:
            _close_preserving_primary(invocation_fd, error, "invocation descriptor")
            raise
        finally:
            cleanup_primary = sys.exception()
            try:
                _cleanup_runtime_resources(
                    cleanup_primary,
                    store=store,
                    descriptors=(
                        () if claim_fd is None else ((claim_fd, "invocation runner claim descriptor"),)
                    ),
                )
            except BaseException as cleanup_error:
                _close_preserving_primary(
                    invocation_fd,
                    cleanup_error,
                    "invocation descriptor",
                )
                raise
        try:
            _transition_boundary("open_return")
            self._authenticate_transition_identity(
                invocation_id,
                composition,
                intent.entrypoint,
                invocation_fd,
                ledger,
            )
            return InvocationHandle(
                invocation_id,
                invocation_root,
                composition.lock_digest,
                intent.entrypoint,
                composition,
                invocation_fd,
                self,
                authorization,
            )
        except BaseException as error:
            _close_preserving_primary(invocation_fd, error, "invocation descriptor")
            raise

    def _continue_start(
        self,
        invocation_id: str,
        composition: FrozenComposition,
        entrypoint: str,
        seed: InvocationSeed,
        authorization: InvocationRuntimeAuthorization,
    ) -> InvocationHandle:
        invocation_fd = self._open_invocation(invocation_id)
        try:
            return self._complete_start_at(
                invocation_id,
                composition,
                entrypoint,
                invocation_fd,
                seed,
                authorization,
            )
        finally:
            _cleanup_runtime_resources(
                sys.exception(),
                store=None,
                descriptors=((invocation_fd, "invocation descriptor"),),
            )

    def _complete_start_at(
        self,
        invocation_id: str,
        composition: FrozenComposition,
        entrypoint: str,
        invocation_fd: int,
        seed: InvocationSeed,
        authorization: InvocationRuntimeAuthorization,
    ) -> InvocationHandle:
        invocation_root = self._invocation_root(invocation_id)
        intent = self._authenticate_invocation_records(
            invocation_id,
            composition,
            entrypoint,
            invocation_fd,
        )
        self._validate_seed_against_intent(seed, intent, authorization)
        _initialization_boundary("before_recovery_root_fsync")
        os.fsync(self._invocations_fd)
        _initialization_boundary("after_recovery_root_fsync")
        intent = self._authenticate_invocation_records(
            invocation_id,
            composition,
            entrypoint,
            invocation_fd,
        )
        preclaim_ledger = Ledger.at(
            invocation_fd,
            "ledger",
            display_root=invocation_root / "ledger",
        )
        preclaim_envelopes = preclaim_ledger.read_all()
        if preclaim_envelopes:
            self._authenticate_bootstrap(
                invocation_id,
                composition,
                preclaim_envelopes,
                intent,
            )
            self._authenticate_invocation_records(
                invocation_id,
                composition,
                entrypoint,
                invocation_fd,
            )
            self._ensure_ledger_durable(preclaim_ledger)
            self._authenticate_transition_identity(
                invocation_id,
                composition,
                entrypoint,
                invocation_fd,
                preclaim_ledger,
            )
        claim_fd = self._acquire_runner_claim(invocation_fd)
        store: SnapshotStore | None = None
        try:
            intent = self._authenticate_invocation_records(
                invocation_id,
                composition,
                entrypoint,
                invocation_fd,
            )
            ledger = Ledger.at(
                invocation_fd,
                "ledger",
                display_root=invocation_root / "ledger",
            )
            envelopes = ledger.read_all()
            if _entry_exists(invocation_fd, "workspace"):
                store = SnapshotStore.at(
                    invocation_fd,
                    "workspace",
                    display_root=invocation_root / "workspace",
                )
            else:
                initial_files = {item.path: item.content for item in seed.workspace.files}
                store = SnapshotStore.create_at(
                    invocation_fd,
                    "workspace",
                    initial_files,
                    display_root=invocation_root / "workspace",
                )
            intent = self._authenticate_invocation_records(
                invocation_id,
                composition,
                entrypoint,
                invocation_fd,
            )
            if envelopes:
                self._authenticate_bootstrap(invocation_id, composition, envelopes, intent)
                self._ensure_ledger_durable(ledger)
                self._authenticate_transition_identity(
                    invocation_id,
                    composition,
                    entrypoint,
                    invocation_fd,
                    ledger,
                )
                projection = fold_events(envelopes)
                self._validate_workflow(composition, projection)
                self._validate_history(composition, envelopes, projection)
                expected_tree_id = projection.head_tree_id or _EMPTY_TREE_ID
                if store.head_tree_id() != expected_tree_id:
                    raise EngineError("workspace HEAD disagrees with the authoritative ledger")
            else:
                if store.head_tree_id() != seed.workspace.tree_id:
                    raise EngineError("unbootstrapped invocation workspace does not match the seed")
                _initialization_boundary("workspace_ready")
                bootstrap = self._bootstrap_events(
                    invocation_id, composition, entrypoint, seed, authorization
                )
                _initialization_boundary("before_ledger_bootstrap")
                envelopes = self._append_authenticated(
                    context="bootstrap",
                    invocation_id=invocation_id,
                    composition=composition,
                    entrypoint=entrypoint,
                    invocation_fd=invocation_fd,
                    ledger=ledger,
                    events=bootstrap,
                    existing=(),
                )
                _initialization_boundary("after_ledger_bootstrap")
                projection = fold_events(envelopes)
                self._validate_workflow(composition, projection)
                self._validate_history(composition, envelopes, projection)
            self._authenticate_transition_identity(
                invocation_id,
                composition,
                entrypoint,
                invocation_fd,
                ledger,
            )
            self._write_checkpoint(invocation_fd, envelopes, projection)
            self._authenticate_transition_identity(
                invocation_id,
                composition,
                entrypoint,
                invocation_fd,
                ledger,
            )
        finally:
            _cleanup_runtime_resources(
                sys.exception(),
                store=store,
                descriptors=((claim_fd, "invocation runner claim descriptor"),),
            )
        _transition_boundary("start_return")
        self._authenticate_transition_identity(
            invocation_id,
            composition,
            entrypoint,
            invocation_fd,
            ledger,
        )
        return InvocationHandle(
            invocation_id,
            invocation_root,
            composition.lock_digest,
            entrypoint,
            composition,
            os.dup(invocation_fd),
            self,
            authorization,
        )

    def _authenticate_bootstrap(
        self,
        invocation_id: str,
        composition: FrozenComposition,
        envelopes: tuple[EventEnvelope, ...],
        intent: InvocationStartIntent,
    ) -> None:
        if not envelopes:
            raise EngineError("invocation has no ledger bootstrap; retry start to initialize it")
        started = envelopes[0].event
        if not isinstance(started, InvocationStarted):
            raise EngineError("invocation ledger lacks its canonical bootstrap")
        if started.event_schema_version != "2":
            raise EngineError("schema-v1 prototype invocation bootstrap is not supported")
        if started.invocation_id != invocation_id:
            raise EngineError("invocation ledger identity does not match its path")
        if started.lock_digest != composition.lock_digest:
            raise InvocationDrift("invocation ledger bootstrap digest differs from its lock")
        if started.entrypoint != intent.entrypoint:
            raise InvocationDrift("invocation ledger bootstrap entrypoint differs from its start intent")
        if started.entrypoint not in composition.workflow.entrypoints:
            raise EngineError("invocation ledger bootstrap uses an unknown entrypoint")
        if (
            started.runtime_authorization_digest != intent.runtime_authorization_digest
            or started.root_input_digest != intent.root_input_digest
            or started.initial_tree_id != intent.initial_tree_id
        ):
            raise InvocationDrift("invocation ledger bootstrap seed identity differs from its start intent")
        if len(envelopes) < 3:
            raise EngineError("invocation ledger lacks its canonical bootstrap")
        graph_id = composition.workflow.entrypoints[started.entrypoint]
        graph = composition.workflow.graphs[graph_id]
        root_input = self._root_input_payload(started.root_input_digest, envelopes[1], envelopes[2])
        expected_graph = GraphStarted(
            graph_instance_id=graph_id,
            graph_id=graph_id,
            input=root_input,
        )
        expected_token = TokenOffered(
            token_id=_start_token_id(graph_id, graph.start),
            graph_instance_id=graph_id,
            source=None,
            target=graph.start,
            payload=root_input,
        )
        if envelopes[1].event != expected_graph:
            raise EngineError("invocation ledger bootstrap root graph input is not canonical")
        if envelopes[2].event != expected_token:
            raise EngineError("invocation ledger bootstrap root graph input is not canonical")

    def _bootstrap_events(
        self,
        invocation_id: str,
        composition: FrozenComposition,
        entrypoint: str,
        seed: InvocationSeed,
        authorization: InvocationRuntimeAuthorization,
    ) -> tuple[RuntimeEvent, ...]:
        graph_id = composition.workflow.entrypoints[entrypoint]
        graph = composition.workflow.graphs[graph_id]
        root_input = freeze_json(seed.root_input)
        return (
            InvocationStarted(
                invocation_id=invocation_id,
                lock_digest=composition.lock_digest,
                entrypoint=entrypoint,
                event_schema_version="2",
                runtime_authorization_digest=authorization.digest,
                root_input_digest=seed.root_input_digest,
                initial_tree_id=seed.workspace.tree_id,
            ),
            GraphStarted(
                graph_instance_id=graph_id,
                graph_id=graph_id,
                input=root_input,
            ),
            TokenOffered(
                token_id=_start_token_id(graph_id, graph.start),
                graph_instance_id=graph_id,
                source=None,
                target=graph.start,
                payload=root_input,
            ),
        )

    def _root_input_payload(
        self,
        root_input_digest: str,
        graph_envelope: EventEnvelope,
        token_envelope: EventEnvelope,
    ) -> FrozenJSONValue:
        graph_event = graph_envelope.event
        token_event = token_envelope.event
        if not isinstance(graph_event, GraphStarted) or not isinstance(token_event, TokenOffered):
            raise EngineError("invocation ledger bootstrap root graph input is not canonical")
        if graph_event.input != token_event.payload:
            raise EngineError("invocation ledger bootstrap root graph input is not canonical")
        expected = canonical_digest(cast(JSONValue, thaw_json(graph_event.input)))
        if root_input_digest != expected:
            raise EngineError("invocation ledger bootstrap root graph input is not canonical")
        return graph_event.input

    def _validate_seed_against_intent(
        self,
        seed: InvocationSeed,
        intent: InvocationStartIntent,
        authorization: InvocationRuntimeAuthorization,
    ) -> None:
        if (
            seed.root_input_digest != intent.root_input_digest
            or seed.workspace.tree_id != intent.initial_tree_id
        ):
            raise InvocationDrift("invocation seed differs from its start intent")
        self._validate_authorization_against_intent(authorization, intent)

    def _validate_authorization_against_intent(
        self,
        authorization: InvocationRuntimeAuthorization,
        intent: InvocationStartIntent,
    ) -> None:
        if authorization.digest != intent.runtime_authorization_digest:
            raise InvocationDrift(
                "invocation start intent authorization differs from the selected authorization"
            )

    def run_until_blocked(self, handle: InvocationHandle) -> RunResult:
        _composition, _invocation_root, invocation_fd = self._validated_handle(handle)
        claim_fd = self._acquire_runner_claim(invocation_fd)
        try:
            return self._run_until_blocked_claimed(handle)
        finally:
            _cleanup_runtime_resources(
                sys.exception(),
                store=None,
                descriptors=((claim_fd, "invocation runner claim descriptor"),),
            )

    def _run_until_blocked_claimed(self, handle: InvocationHandle) -> RunResult:
        composition, invocation_root, invocation_fd = self._validated_handle(handle)
        with handle.workspace as store:
            return self._run_until_blocked_with_store(
                handle,
                composition,
                invocation_root,
                invocation_fd,
                store,
            )

    def _run_until_blocked_with_store(
        self,
        handle: InvocationHandle,
        composition: FrozenComposition,
        invocation_root: Path,
        invocation_fd: int,
        store: SnapshotStore,
    ) -> RunResult:
        ledger = Ledger.at(
            invocation_fd,
            "ledger",
            display_root=invocation_root / "ledger",
        )
        store.head_tree_id()
        scheduler = self._scheduler(
            handle.invocation_id,
            composition,
            handle._entrypoint,
            invocation_root,
            invocation_fd,
            store,
            ledger,
            handle._authorization,
        )
        executor = EffectExecutor(
            composition.registries.effects,
            composition.registries.schemas,
            ledger,
            transition_guard=lambda: self._authenticate_transition_identity(
                handle.invocation_id,
                composition,
                handle._entrypoint,
                invocation_fd,
                ledger,
            ),
        )

        recovered = False
        while True:
            envelopes = ledger.read_all()
            projection = fold_events(envelopes)
            self._validate_projection_identity(handle, projection)
            self._validate_workflow(composition, projection)
            result = self._run_result(projection)
            if result is not None:
                self._write_checkpoint(invocation_fd, envelopes, projection)
                return result

            if not recovered:
                if projection.status == "running":
                    try:
                        asyncio.run(
                            scheduler.recover_live_activities(
                                plan_running_tasks(composition.workflow, projection)
                            )
                        )
                    except LedgerConflictError as error:
                        raise EngineConflictError("another runner advanced the invocation") from error
                    except LedgerPublicationIndeterminate as error:
                        raise EnginePublicationIndeterminate(
                            "activity recovery publication is indeterminate"
                        ) from error
                recovered = True
                continue

            try:
                if scheduler.reclaim_expired():
                    continue
            except LedgerConflictError as error:
                raise EngineConflictError("another runner advanced the invocation") from error
            except LedgerPublicationIndeterminate as error:
                raise EnginePublicationIndeterminate(
                    "lease reclamation publication is indeterminate"
                ) from error

            if needs_settlement(projection):
                try:
                    settlement = asyncio.run(executor.settle_next(projection))
                except LedgerConflictError as error:
                    raise EngineConflictError("another runner advanced the invocation") from error
                except (LedgerPublicationIndeterminate, EffectPublicationIndeterminate) as error:
                    raise EnginePublicationIndeterminate("effect publication is indeterminate") from error
                except EffectStateError as error:
                    raise EngineError(str(error)) from error
                if settlement.progressed:
                    continue
                if settlement.pending:
                    refreshed = fold_events(ledger.read_all())
                    self._validate_workflow(composition, refreshed)
                    self._write_checkpoint(invocation_fd, ledger.read_all(), refreshed)
                    return RunResult(
                        status="interrupted",
                        terminal_reason="effect_pending",
                        actions=(),
                        projection=refreshed,
                    )
                raise EngineError("effect settlement made no progress")

            running_tasks = plan_running_tasks(composition.workflow, projection)
            if running_tasks:
                try:
                    resumed = asyncio.run(scheduler.resume_running(running_tasks))
                except (LedgerConflictError, LeaseUnavailableError) as error:
                    raise EngineConflictError("another runner owns or advanced the running task") from error
                except LedgerPublicationIndeterminate as error:
                    raise EnginePublicationIndeterminate(
                        "running task publication is indeterminate"
                    ) from error
                except HeadPublicationIndeterminate as error:
                    raise EnginePublicationIndeterminate(
                        "running task publication is indeterminate"
                    ) from error
                except FinalizationRolledBack as error:
                    raise EngineError("running task publication failed and was rolled back") from error
                if resumed:
                    continue
                if any(scheduler._task_has_live_activity(task) for task in running_tasks):
                    refreshed = fold_events(ledger.read_all())
                    self._validate_workflow(composition, refreshed)
                    self._write_checkpoint(invocation_fd, ledger.read_all(), refreshed)
                    return RunResult(
                        status="interrupted",
                        terminal_reason="activity_recovery",
                        actions=(),
                        projection=refreshed,
                    )
                continue

            plan = plan_next(composition.workflow, projection)
            if plan.events:
                self._append_authenticated(
                    context="planner_append",
                    invocation_id=handle.invocation_id,
                    composition=composition,
                    entrypoint=handle._entrypoint,
                    invocation_fd=invocation_fd,
                    ledger=ledger,
                    events=plan.events,
                    existing=envelopes,
                )
                continue
            if plan.tasks:
                try:
                    asyncio.run(scheduler.run_wave(plan.tasks))
                except LedgerConflictError as error:
                    raise EngineConflictError("another runner advanced the invocation") from error
                except LedgerPublicationIndeterminate as error:
                    raise EnginePublicationIndeterminate("task publication is indeterminate") from error
                except HeadPublicationIndeterminate as error:
                    raise EnginePublicationIndeterminate("task publication is indeterminate") from error
                except FinalizationRolledBack as error:
                    raise EngineError("task publication failed and was rolled back") from error
                continue
            if plan.terminal == "interrupted":
                refreshed = fold_events(ledger.read_all())
                self._validate_workflow(composition, refreshed)
                result = self._run_result(refreshed)
                if result is not None:
                    return result
            raise EngineError("running invocation has no planned transition or runnable task")

    def _acquire_runner_claim(self, invocation_fd: int) -> int:
        try:
            descriptor = os.open(
                _RUNNER_LOCK,
                os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=invocation_fd,
            )
        except OSError as error:
            raise EngineError("cannot open the invocation runner claim") from error
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
                raise EngineError("invocation runner claim is not a stable regular file")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise EngineConflictError("another runner holds the invocation claim") from error
            locked = os.fstat(descriptor)
            try:
                current = os.stat(
                    _RUNNER_LOCK,
                    dir_fd=invocation_fd,
                    follow_symlinks=False,
                )
            except OSError as error:
                raise EngineError("invocation runner claim lost its stable anchor") from error
            if (
                not stat.S_ISREG(locked.st_mode)
                or locked.st_nlink != 1
                or not stat.S_ISREG(current.st_mode)
                or current.st_nlink != 1
                or (locked.st_dev, locked.st_ino) != (current.st_dev, current.st_ino)
            ):
                raise EngineError("invocation runner claim lost its stable anchor")
            return descriptor
        except BaseException:
            _close_preserving_primary(
                descriptor,
                sys.exception(),
                "invocation runner claim descriptor",
            )
            raise

    def resume(
        self,
        handle: InvocationHandle,
        *,
        action: str,
        payload: JSONValue,
    ) -> InvocationHandle:
        _composition, _invocation_root, invocation_fd = self._validated_handle(handle)
        claim_fd = self._acquire_runner_claim(invocation_fd)
        try:
            return self._resume_claimed(handle, action=action, payload=payload)
        finally:
            _cleanup_runtime_resources(
                sys.exception(),
                store=None,
                descriptors=((claim_fd, "invocation runner claim descriptor"),),
            )

    def _resume_claimed(
        self,
        handle: InvocationHandle,
        *,
        action: str,
        payload: JSONValue,
    ) -> InvocationHandle:
        composition, invocation_root, invocation_fd = self._validated_handle(handle)
        ledger = Ledger.at(
            invocation_fd,
            "ledger",
            display_root=invocation_root / "ledger",
        )
        envelopes = ledger.read_all()
        projection = fold_events(envelopes)
        self._validate_projection_identity(handle, projection)
        self._validate_workflow(composition, projection)
        pending = projection.pending_interrupt
        if projection.status != "running" or pending is None:
            raise EngineError("invocation has no pending interrupt")
        if action not in pending.actions:
            raise EngineError(f"resume action is not allowed: {action!r}")
        activation = next(
            item for item in projection.activations if item.activation_id == pending.activation_id
        )
        graph = next(
            item
            for item in projection.graph_instances
            if item.graph_instance_id == activation.graph_instance_id
        )
        node = composition.workflow.graphs[graph.graph_id].nodes[activation.node_id]
        if node.definition.kind != "interrupt":
            raise EngineError("pending interrupt does not reference an interrupt node")
        output: JSONValue = {"action": action, "payload": payload}
        events: tuple[RuntimeEvent, ...] = (
            InterruptResumed(interrupt_id=pending.interrupt_id, action=action, payload=payload),
            NodeCompleted(activation_id=activation.activation_id, output=output),
        )
        refreshed = self._append_authenticated(
            context="resume_append",
            invocation_id=handle.invocation_id,
            composition=composition,
            entrypoint=handle._entrypoint,
            invocation_fd=invocation_fd,
            ledger=ledger,
            events=events,
            existing=envelopes,
        )
        refreshed_projection = fold_events(refreshed)
        self._validate_workflow(composition, refreshed_projection)
        self._write_checkpoint(invocation_fd, refreshed, refreshed_projection)
        return InvocationHandle(
            handle.invocation_id,
            invocation_root,
            handle.lock_digest,
            handle._entrypoint,
            composition,
            os.dup(invocation_fd),
            self,
            handle._authorization,
        )

    def _scheduler(
        self,
        invocation_id: str,
        composition: FrozenComposition,
        entrypoint: str,
        invocation_root: Path,
        invocation_fd: int,
        store: SnapshotStore,
        ledger: Ledger,
        authorization: InvocationRuntimeAuthorization,
    ) -> Scheduler:
        owner_id = canonical_digest(
            {
                "invocation_id": invocation_id,
                "lock_digest": composition.lock_digest,
                "role": "engine-scheduler",
            }
        )
        receipts = TerminalReceiptStore.open_or_create_at(
            invocation_fd,
            "receipts",
            display_root=invocation_root / "receipts",
        )
        return Scheduler(
            composition.registries.capabilities,
            store,
            ledger,
            self._host or _UnavailableTaskHost(),
            owner_id=owner_id,
            clock=self._clock,
            transition_guard=lambda: self._authenticate_transition_identity(
                invocation_id,
                composition,
                entrypoint,
                invocation_fd,
                ledger,
            ),
            lock_digest=composition.lock_digest,
            composition_digest=composition.digest,
            entrypoint=entrypoint,
            effects=composition.registries.effects,
            schemas=composition.registries.schemas,
            resources=composition.registries.resources,
            receipts=receipts,
            runtime_authorization=authorization,
        )

    async def _recover_invocation(self, handle: InvocationHandle) -> RecoveryResult:
        _composition, _invocation_root, invocation_fd = self._validated_handle(handle)
        claim_fd = self._acquire_runner_claim(invocation_fd)
        try:
            return await self._recover_invocation_claimed(handle)
        finally:
            _cleanup_runtime_resources(
                sys.exception(),
                store=None,
                descriptors=((claim_fd, "invocation runner claim descriptor"),),
            )

    async def _recover_invocation_claimed(self, handle: InvocationHandle) -> RecoveryResult:
        composition, invocation_root, invocation_fd = self._validated_handle(handle)
        with handle.workspace as store:
            ledger = Ledger.at(
                invocation_fd,
                "ledger",
                display_root=invocation_root / "ledger",
            )
            scheduler = self._scheduler(
                handle.invocation_id,
                composition,
                handle._entrypoint,
                invocation_root,
                invocation_fd,
                store,
                ledger,
                handle._authorization,
            )
            envelopes = ledger.read_all()
            projection = fold_events(envelopes)
            self._validate_projection_identity(handle, projection)
            self._validate_workflow(composition, projection)
            if projection.status != "running":
                return RecoveryResult()
            try:
                return await scheduler.recover_live_activities(
                    plan_running_tasks(composition.workflow, projection)
                )
            except LedgerConflictError as error:
                raise EngineConflictError("another runner advanced the invocation") from error
            except LedgerPublicationIndeterminate as error:
                raise EnginePublicationIndeterminate(
                    "activity recovery publication is indeterminate"
                ) from error

    def _authenticate_live_activity_workspaces(
        self,
        projection: InvocationProjection,
        store: SnapshotStore,
    ) -> None:
        for activation in projection.activations:
            if not activation.attempts:
                continue
            attempt = activation.attempts[-1]
            if attempt.status not in {"running", "effect_pending"} or attempt.activity is None:
                continue
            store.open_attempt(attempt.activity.workspace_identity)

    def _require_invocation_anchor(self, invocation_id: str, invocation_fd: int) -> None:
        self._assert_namespace_path_current()
        try:
            current = os.stat(
                invocation_id,
                dir_fd=self._invocations_fd,
                follow_symlinks=False,
            )
            opened = os.fstat(invocation_fd)
        except OSError as error:
            raise InvocationDrift("invocation directory lost its final-name anchor") from error
        if (
            not stat.S_ISDIR(current.st_mode)
            or not stat.S_ISDIR(opened.st_mode)
            or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise InvocationDrift("invocation directory identity differs from its final-name anchor")

    def _authenticate_invocation_records(
        self,
        invocation_id: str,
        composition: FrozenComposition,
        entrypoint: str | None,
        invocation_fd: int,
    ) -> InvocationStartIntent:
        self._require_invocation_anchor(invocation_id, invocation_fd)
        authenticate_invocation_lock(invocation_fd, composition.lock)
        intent = authenticate_invocation_start_intent(
            invocation_fd,
            lock_digest=composition.lock_digest,
            entrypoint=entrypoint,
        )
        self._require_invocation_anchor(invocation_id, invocation_fd)
        return intent

    def _authenticate_transition_identity(
        self,
        invocation_id: str,
        composition: FrozenComposition,
        entrypoint: str,
        invocation_fd: int,
        ledger: Ledger,
    ) -> None:
        intent = self._authenticate_invocation_records(
            invocation_id,
            composition,
            entrypoint,
            invocation_fd,
        )
        envelopes = ledger.read_bootstrap()
        self._authenticate_bootstrap(
            invocation_id,
            composition,
            envelopes,
            intent,
        )
        self._authenticate_invocation_records(
            invocation_id,
            composition,
            entrypoint,
            invocation_fd,
        )

    def _validated_handle(self, handle: InvocationHandle) -> tuple[FrozenComposition, Path, int]:
        self._assert_namespace_path_current()
        if handle._closed:
            raise EngineError("invocation handle is closed")
        expected_root = self._invocation_root(handle.invocation_id)
        if handle.invocation_root.absolute() != expected_root:
            raise EngineError("invocation handle root is outside the engine namespace")
        composition = handle._composition
        if composition.lock_digest != handle.lock_digest:
            raise EngineError("invocation handle lock digest mismatch")
        ledger = Ledger.at(
            handle._invocation_fd,
            "ledger",
            display_root=expected_root / "ledger",
        )
        self._authenticate_transition_identity(
            handle.invocation_id,
            composition,
            handle._entrypoint,
            handle._invocation_fd,
            ledger,
        )
        return composition, expected_root, handle._invocation_fd

    def _assert_namespace_path_current(self) -> None:
        if self._closed:
            raise EngineError("engine is closed")
        try:
            current_fd = _open_absolute_directory(self._invocations_root)
        except (OSError, EngineError) as error:
            raise EngineError("invocation namespace no longer names the trusted directory") from error
        try:
            current = os.fstat(current_fd)
            trusted = os.fstat(self._invocations_fd)
            if (current.st_dev, current.st_ino) != (trusted.st_dev, trusted.st_ino):
                raise EngineError("invocation namespace identity changed")
        finally:
            os.close(current_fd)

    def _validate_workflow(
        self,
        composition: FrozenComposition,
        projection: InvocationProjection,
    ) -> None:
        try:
            validate_projection(composition.workflow, projection)
        except PlanningError as error:
            raise EngineError(f"invocation does not match frozen composition workflow: {error}") from error

    def _validate_history(
        self,
        composition: FrozenComposition,
        envelopes: tuple[EventEnvelope, ...],
        projection: InvocationProjection,
    ) -> None:
        try:
            validate_event_history(composition.workflow, envelopes, projection)
        except PlanningError as error:
            raise EngineError(f"invocation event history is invalid: {error}") from error

    def _validate_projection_identity(
        self, handle: InvocationHandle, projection: InvocationProjection
    ) -> None:
        if (
            projection.invocation_id != handle.invocation_id
            or projection.lock_digest != handle.lock_digest
            or projection.entrypoint != handle._entrypoint
        ):
            raise EngineError("invocation handle does not match the authoritative ledger")

    def _invocation_root(self, invocation_id: str) -> Path:
        windows = PureWindowsPath(invocation_id)
        if (
            not invocation_id
            or "\x00" in invocation_id
            or invocation_id in {".", ".."}
            or "/" in invocation_id
            or "\\" in invocation_id
            or windows.is_absolute()
            or bool(windows.drive)
            or os.path.sep in invocation_id
            or (os.path.altsep is not None and os.path.altsep in invocation_id)
        ):
            raise EngineError(f"invalid invocation id: {invocation_id!r}")
        return self._invocations_root / invocation_id

    def _open_invocation(self, invocation_id: str) -> int:
        self._invocation_root(invocation_id)
        try:
            return _open_directory_at(
                self._invocations_fd,
                invocation_id,
                "invocation",
            )
        except FileNotFoundError as error:
            raise EngineError(f"invocation does not exist: {invocation_id}") from error

    def _install_invocation(
        self,
        staging_name: str,
        invocation_id: str,
        invocation_fd: int,
    ) -> bool:
        lock_fd = os.open(
            ".namespace.lock",
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=self._invocations_fd,
        )
        try:
            _require_stable_lock_anchor(
                self._invocations_fd,
                ".namespace.lock",
                lock_fd,
                kind="invocation namespace lock",
            )
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            _require_stable_lock_anchor(
                self._invocations_fd,
                ".namespace.lock",
                lock_fd,
                kind="invocation namespace lock",
            )
            if _entry_exists(self._invocations_fd, invocation_id):
                return False
            _initialization_boundary("before_invocation_rename")
            try:
                _rename_no_replace_at(
                    self._invocations_fd,
                    staging_name,
                    invocation_id,
                )
            except FileExistsError:
                return False
            _initialization_boundary("after_invocation_rename")
            self._require_invocation_anchor(invocation_id, invocation_fd)
            _initialization_boundary("before_root_fsync")
            os.fsync(self._invocations_fd)
            _initialization_boundary("after_root_fsync")
            self._require_invocation_anchor(invocation_id, invocation_fd)
            return True
        finally:
            _close_preserving_primary(
                lock_fd,
                sys.exception(),
                "invocation namespace lock descriptor",
            )

    def _append_authenticated(
        self,
        *,
        context: Literal["bootstrap", "planner_append", "resume_append"],
        invocation_id: str,
        composition: FrozenComposition,
        entrypoint: str,
        invocation_fd: int,
        ledger: Ledger,
        events: tuple[RuntimeEvent, ...],
        existing: tuple[EventEnvelope, ...],
    ) -> tuple[EventEnvelope, ...]:
        _transition_boundary(context)
        if context == "bootstrap":
            self._authenticate_invocation_records(
                invocation_id,
                composition,
                entrypoint,
                invocation_fd,
            )
        else:
            self._authenticate_transition_identity(
                invocation_id,
                composition,
                entrypoint,
                invocation_fd,
                ledger,
            )
        expected_next_seq = existing[-1].seq + 1 if existing else 1
        try:
            append_validated_batch(ledger, events, expected_next_seq=expected_next_seq)
        except LedgerConflictError as error:
            raise EngineConflictError("another runner advanced the invocation") from error
        except LedgerPublicationIndeterminate as error:
            raise EnginePublicationIndeterminate("ledger publication outcome is indeterminate") from error
        if context == "bootstrap":
            refreshed = ledger.read_all()
            intent = self._authenticate_invocation_records(
                invocation_id,
                composition,
                entrypoint,
                invocation_fd,
            )
            self._authenticate_bootstrap(
                invocation_id,
                composition,
                refreshed,
                intent,
            )
            self._authenticate_invocation_records(
                invocation_id,
                composition,
                entrypoint,
                invocation_fd,
            )
            return refreshed
        self._authenticate_transition_identity(
            invocation_id,
            composition,
            entrypoint,
            invocation_fd,
            ledger,
        )
        return ledger.read_all()

    def _ensure_ledger_durable(self, ledger: Ledger) -> None:
        try:
            ledger.ensure_durable()
        except OSError as error:
            raise EnginePublicationIndeterminate(
                "authoritative ledger durability is indeterminate"
            ) from error

    def _write_checkpoint(
        self,
        invocation_fd: int,
        envelopes: tuple[EventEnvelope, ...],
        projection: InvocationProjection,
    ) -> None:
        try:
            write_checkpoint_at(
                invocation_fd,
                "checkpoint.json",
                projection,
                envelopes[-1].seq if envelopes else 0,
                ledger_envelopes=envelopes,
            )
        except (OSError, ValueError):
            pass

    def _run_result(self, projection: InvocationProjection) -> RunResult | None:
        pending = projection.pending_interrupt
        if pending is not None:
            return RunResult(
                status="interrupted",
                terminal_reason=pending.reason,
                actions=pending.actions,
                projection=projection,
            )
        if projection.status == "running":
            return None
        if projection.status == "not_started":
            raise EngineError("authoritative ledger has not started the invocation")
        root = next(
            (item for item in projection.graph_instances if item.parent_graph_instance_id is None),
            None,
        )
        return RunResult(
            status=projection.status,
            output=None if root is None else root.output,
            terminal_reason=projection.terminal_reason,
            projection=projection,
        )


_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


def _open_or_create_namespace(root: Path) -> int:
    try:
        root_fd = _open_or_create_absolute_directory(root)
        try:
            try:
                os.mkdir("invocations", mode=0o700, dir_fd=root_fd)
                os.fsync(root_fd)
            except FileExistsError:
                pass
            return _open_directory_at(root_fd, "invocations", "invocation namespace")
        finally:
            os.close(root_fd)
    except (OSError, EngineError) as error:
        if isinstance(error, EngineError):
            raise
        raise EngineError("cannot establish trusted invocation namespace") from error


def _open_or_create_absolute_directory(path: Path) -> int:
    descriptor = os.open("/", _DIRECTORY_FLAGS)
    try:
        for component in path.absolute().parts[1:]:
            try:
                child = _open_directory_at(descriptor, component, "engine root component")
            except FileNotFoundError:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=descriptor)
                    os.fsync(descriptor)
                except FileExistsError:
                    pass
                child = _open_directory_at(descriptor, component, "engine root component")
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_absolute_directory(path: Path) -> int:
    descriptor = os.open("/", _DIRECTORY_FLAGS)
    try:
        for component in path.absolute().parts[1:]:
            child = _open_directory_at(descriptor, component, "engine root component")
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_directory_at(parent_fd: int, name: str, kind: str) -> int:
    try:
        enumerated = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError:
        raise
    except OSError as error:
        raise EngineError(f"{kind} must be a trusted no-follow directory: {name}") from error
    opened = os.fstat(descriptor)
    if not stat.S_ISDIR(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
        enumerated.st_dev,
        enumerated.st_ino,
    ):
        os.close(descriptor)
        raise EngineError(f"{kind} identity changed while opening: {name}")
    return descriptor


def _entry_exists(parent_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _entry_matches_descriptor(parent_fd: int, name: str, descriptor: int) -> bool:
    try:
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        opened = os.fstat(descriptor)
    except OSError:
        return False
    return (
        stat.S_ISDIR(current.st_mode)
        and stat.S_ISDIR(opened.st_mode)
        and (current.st_dev, current.st_ino) == (opened.st_dev, opened.st_ino)
    )


def _require_stable_lock_anchor(
    parent_fd: int,
    name: str,
    descriptor: int,
    *,
    kind: str,
) -> None:
    try:
        opened = os.fstat(descriptor)
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as error:
        raise EngineError(f"{kind} lost its stable anchor") from error
    if (
        not stat.S_ISREG(opened.st_mode)
        or opened.st_nlink != 1
        or not stat.S_ISREG(current.st_mode)
        or current.st_nlink != 1
        or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
    ):
        raise EngineError(f"{kind} is not a stable regular file with one link")


def _remove_entry_at(parent_fd: int, name: str) -> None:
    entry = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if not stat.S_ISDIR(entry.st_mode) or stat.S_ISLNK(entry.st_mode):
        os.unlink(name, dir_fd=parent_fd)
        return
    child_fd = _open_directory_at(parent_fd, name, "owned initialization staging")
    try:
        for child_name in os.listdir(child_fd):
            _remove_entry_at(child_fd, child_name)
        os.fsync(child_fd)
    finally:
        _close_preserving_primary(
            child_fd,
            sys.exception(),
            "owned initialization staging child descriptor",
        )
    os.rmdir(name, dir_fd=parent_fd)


def _close_preserving_primary(
    descriptor: int,
    primary: BaseException | None,
    kind: str,
) -> None:
    try:
        os.close(descriptor)
    except BaseException as close_error:
        if primary is None:
            raise
        primary.add_note(f"{kind} close failed: {close_error}")


def _cleanup_runtime_resources(
    primary: BaseException | None,
    *,
    store: SnapshotStore | None,
    descriptors: tuple[tuple[int, str], ...],
    actions: tuple[tuple[Callable[[], None], str], ...] = (),
) -> None:
    cleanup_failure: BaseException | None = None

    def record(error: BaseException, kind: str) -> None:
        nonlocal cleanup_failure
        if primary is not None:
            primary.add_note(f"{kind} cleanup failed: {error}")
        elif cleanup_failure is None:
            cleanup_failure = error
        else:
            cleanup_failure.add_note(f"{kind} cleanup failed: {error}")

    if store is not None:
        try:
            store.close()
        except BaseException as error:
            record(error, "invocation workspace store")
    for descriptor, kind in descriptors:
        try:
            os.close(descriptor)
        except BaseException as error:
            record(error, kind)
    for action, kind in actions:
        try:
            action()
        except BaseException as error:
            record(error, kind)
    if primary is None and cleanup_failure is not None:
        raise cleanup_failure


def _cleanup_staging(
    primary: BaseException | None,
    *,
    parent_fd: int,
    name: str,
    descriptor: int | None,
) -> None:
    def remove() -> None:
        if _entry_exists(parent_fd, name):
            _remove_entry_at(parent_fd, name)

    _cleanup_runtime_resources(
        primary,
        store=None,
        descriptors=(
            () if descriptor is None else ((descriptor, "invocation initialization staging descriptor"),)
        ),
        actions=(
            (remove, "invocation initialization staging removal"),
            (lambda: os.fsync(parent_fd), "invocation namespace fsync"),
        ),
    )


def _initialization_boundary(name: str) -> None:
    del name


def _transition_boundary(name: str) -> None:
    del name


__all__ = [
    "Engine",
    "EngineConflictError",
    "EngineError",
    "EnginePublicationIndeterminate",
    "InvocationHandle",
    "RecoveryResult",
    "RunResult",
]
