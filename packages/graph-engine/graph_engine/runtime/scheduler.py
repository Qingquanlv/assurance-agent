from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, cast

from pydantic import BaseModel, ConfigDict, Field

from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.composition.lock import pinned_execution_host_lock
from graph_engine.composition.models import EffectRegistry, SchemaRegistry
from graph_engine.errors import GraphEngineError
from graph_engine.identifiers import canonical_id
from graph_engine.plugin_api import (
    AttemptWorkspaceIdentity,
    CandidateWriteSet,
    CommitValidator,
    EffectIntent,
    FailureKind,
    InvocationMetadata,
    RecoverableTaskHandler,
    ResourceClaims,
    TaskActivityReconcileResult,
    TaskActivitySnapshot,
    TaskHandler,
    TaskOutcome,
    TaskRequest,
    ValidationContext,
)
from graph_engine.runtime.activity import (
    LedgerTaskActivityPort,
    TaskActivityRecoveryUnsupported,
    recovery_decision_for_status,
    write_set_digest,
)
from graph_engine.runtime.host_protocol import (
    AttemptRootDescriptor,
    HostOperation,
    TaskActivityRpcIdentity,
    TaskExecutionHost,
    TaskHostCallIdentity,
    TaskHostCallResult,
    TaskHostCancelCall,
    TaskHostExecuteCall,
    TaskHostReconcileCall,
    TaskHostTerminalReceipt,
)
from graph_engine.runtime.host_receipts import TerminalReceiptError, TerminalReceiptStore
from graph_engine.runtime.events import (
    EffectIntentCommitted,
    EventEnvelope,
    HeadAdvanced,
    RuntimeEvent,
    TaskActivityCancelRequested,
    TaskActivityPrepared,
    TaskActivityTerminalObserved,
    TaskAttemptFailed,
    TaskAttemptStarted,
    TaskAttemptStopped,
    TaskAttemptSucceeded,
    TaskCommitPrepared,
    TaskLeaseAcquired,
    TaskLeaseAdopted,
    TaskLeaseHeartbeat,
)
from graph_engine.runtime.frozen_json import thaw_json
from graph_engine.runtime.json_schema import match_json_schema, validate_json_schema
from graph_engine.runtime.ledger import (
    Ledger,
    LedgerPublicationIndeterminate,
    append_validated_batch,
)
from graph_engine.runtime.models import (
    ActivityRecoveryDecision,
    AttemptRecord,
    CommitResult,
    InvocationProjection,
    PlannedTask,
    ProjectionError,
    RecoveryResult,
    activity_id_for_attempt,
    attempt_directory_id,
    fold_events,
)
from graph_engine.runtime.workspace import (
    AttemptWorkspace,
    FinalizationRolledBack,
    HeadPublicationIndeterminate,
    SnapshotStore,
)

_match_json_schema = match_json_schema
_validate_json_schema = validate_json_schema
_PROMOTING_TERMINAL = False


def _promotion_cut(name: str) -> None:
    del name


class SchedulerStateError(GraphEngineError):
    """Raised when persisted state rejects a requested scheduler transition."""


class LeaseUnavailableError(SchedulerStateError):
    """Raised when a task no longer owns a live persisted lease."""


class Clock(Protocol):
    def now(self) -> float: ...


class _CapabilityRegistryView(Protocol):
    @property
    def task_handlers(self) -> Mapping[str, TaskHandler]: ...

    @property
    def commit_validators(self) -> Mapping[str, CommitValidator]: ...

    @property
    def bindings(self) -> Mapping[str, object]: ...


class SystemClock:
    def now(self) -> float:
        return time.time()


@dataclass(slots=True)
class FakeClock:
    current: float

    def now(self) -> float:
        return self.current

    def set(self, value: float) -> None:
        self.current = value

    def advance(self, seconds: float) -> None:
        self.current += seconds


_FROZEN = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class Lease(BaseModel):
    model_config = _FROZEN

    task_id: str
    activation_id: str
    attempt: int = Field(ge=1)
    owner_id: str = Field(min_length=1)
    acquired_at: float
    heartbeat_at: float
    expires_at: float

    def model_post_init(self, __context: object) -> None:
        if not self.acquired_at <= self.heartbeat_at <= self.expires_at:
            raise ValueError("lease timestamps are inconsistent")


class AttemptResult(BaseModel):
    model_config = _FROZEN

    task: PlannedTask
    outcome: TaskOutcome
    lease: Lease
    candidate: CandidateWriteSet | None = None
    commit: CommitResult | None = None
    head_tree_id: str | None = None
    effect_ids: tuple[str, ...] = ()


@dataclass(slots=True)
class _LeaseState:
    scheduler: Scheduler
    current: Lease

    def heartbeat(self) -> None:
        self.current = self.scheduler.heartbeat(self.current)


@dataclass(frozen=True, slots=True)
class _LeaseGuard:
    expected_next_seq: int
    running: Lease | None


def _overlaps(first: str, second: str) -> bool:
    return first == second or first.startswith(f"{second}/") or second.startswith(f"{first}/")


def _sets_overlap(first: tuple[str, ...], second: tuple[str, ...]) -> bool:
    return any(_overlaps(left, right) for left in first for right in second)


def _claims_conflict(first: ResourceClaims, second: ResourceClaims) -> bool:
    return any(
        (
            bool(first.exclusive),
            bool(second.exclusive),
            _sets_overlap(first.writes, second.writes),
            _sets_overlap(first.writes, second.reads),
            _sets_overlap(second.writes, first.reads),
        )
    )


def _task_order(task: PlannedTask) -> tuple[int, int, str]:
    return task.topology_rank, task.declaration_index, task.task_id


def select_wave(tasks: Sequence[PlannedTask], max_parallel: int) -> tuple[PlannedTask, ...]:
    if not isinstance(max_parallel, int) or isinstance(max_parallel, bool) or max_parallel < 1:
        raise ValueError("max_parallel must be a positive integer")
    selected: list[PlannedTask] = []
    for task in sorted(tasks, key=_task_order):
        if any(_claims_conflict(task.resources, other.resources) for other in selected):
            break
        selected.append(task)
        if len(selected) == max_parallel:
            break
    return tuple(selected)


class Scheduler:
    def __init__(
        self,
        registry: _CapabilityRegistryView,
        store: SnapshotStore,
        ledger: Ledger,
        host: TaskExecutionHost,
        *,
        owner_id: str,
        clock: Clock | None = None,
        lease_seconds: float = 30.0,
        max_parallel: int = 1,
        transition_guard: Callable[[], None] | None = None,
        lock_digest: str | None = None,
        composition_digest: str | None = None,
        entrypoint: str | None = None,
        effects: EffectRegistry | None = None,
        schemas: SchemaRegistry | None = None,
        resources: object | None = None,
        receipts: TerminalReceiptStore | None = None,
        cancel_timeout_seconds: float = 5.0,
    ) -> None:
        if not owner_id:
            raise ValueError("owner_id must not be empty")
        if not math.isfinite(lease_seconds) or lease_seconds <= 0:
            raise ValueError("lease_seconds must be finite and positive")
        if not math.isfinite(cancel_timeout_seconds) or cancel_timeout_seconds <= 0:
            raise ValueError("cancel_timeout_seconds must be finite and positive")
        if not isinstance(max_parallel, int) or isinstance(max_parallel, bool) or max_parallel < 1:
            raise ValueError("max_parallel must be a positive integer")
        self._registry = registry
        self._store = store
        self._ledger = ledger
        self._host = host
        self._owner_id = owner_id
        self._clock = clock or SystemClock()
        self._lease_seconds = lease_seconds
        self._max_parallel = max_parallel
        self._transition_guard = transition_guard
        self._lock_digest = lock_digest
        self._composition_digest = composition_digest
        self._entrypoint = entrypoint
        self._effects = effects if effects is not None else EffectRegistry({})
        self._schemas = schemas if schemas is not None else SchemaRegistry({})
        self._resources = resources
        self._receipts = receipts
        self._cancel_timeout_seconds = cancel_timeout_seconds
        self._same_attempt_execute: set[tuple[str, int]] = set()
        bind_runtime = getattr(host, "bind_invocation_runtime", None)
        if callable(bind_runtime):
            try:
                bind_runtime(handlers=registry.task_handlers, store=store, receipts=receipts)
            except TypeError:
                bind_runtime(handlers=registry.task_handlers, store=store)

    def task_activity_port(self, identity: TaskActivityRpcIdentity) -> LedgerTaskActivityPort:
        """Return a CAS port bound to one activity identity on this invocation ledger."""
        if identity.activity_id is None:
            raise SchedulerStateError("activity port requires an activity id")
        return LedgerTaskActivityPort(
            ledger=self._ledger,
            identity=identity,
            transition_guard=self._guard_transition,
        )

    def start_recoverable(
        self, task: PlannedTask
    ) -> tuple[Lease, AttemptWorkspace, AttemptWorkspaceIdentity]:
        handler = self._registry.task_handlers.get(task.capability_id)
        if not isinstance(handler, RecoverableTaskHandler):
            raise TaskActivityRecoveryUnsupported(f"task handler is not recoverable: {task.capability_id}")
        envelopes = self._ledger.read_all()
        _validate_start_transition(task, envelopes)
        request = self._project_request(task)
        request_digest = canonical_digest(cast(JSONValue, request.model_dump(mode="json")))
        directory_id = attempt_directory_id(
            task.invocation_id,
            task.task_id,
            task.activation_id,
            task.attempt,
        )
        self._store.discard_unprepared_orphan(directory_id)
        workspace, identity = self._store.create_attempt_identity(
            invocation_id=task.invocation_id,
            task_id=task.task_id,
            activation_id=task.activation_id,
            attempt=task.attempt,
        )
        acquired = self._now()
        lease = Lease(
            task_id=task.task_id,
            activation_id=task.activation_id,
            attempt=task.attempt,
            owner_id=self._owner_id,
            acquired_at=acquired,
            heartbeat_at=acquired,
            expires_at=acquired + self._lease_seconds,
        )
        events: tuple[RuntimeEvent, ...] = (
            TaskAttemptStarted(
                activation_id=task.activation_id,
                attempt=task.attempt,
                lease_expires_at=format(lease.expires_at, ".17g"),
            ),
            TaskLeaseAcquired(**lease.model_dump()),
            TaskActivityPrepared(
                activity_id=activity_id_for_attempt(
                    task.invocation_id,
                    task.task_id,
                    task.activation_id,
                    task.attempt,
                ),
                task_id=task.task_id,
                activation_id=task.activation_id,
                attempt=task.attempt,
                request_digest=request_digest,
                workspace_identity=identity,
            ),
        )
        expected_next_seq = _next_sequence(envelopes)
        expected = tuple(
            EventEnvelope.from_event(expected_next_seq + offset, event) for offset, event in enumerate(events)
        )
        try:
            self._append(events, expected_next_seq=expected_next_seq)
        except LedgerPublicationIndeterminate:
            raise
        except BaseException:
            if _initial_batch_absent(self._ledger, expected, expected_next_seq):
                workspace.discard()
            raise
        return lease, workspace, identity

    async def recover_activity(self, task: PlannedTask) -> ActivityRecoveryDecision:
        """Reconcile one live recoverable activity before any reclaim or new attempt."""
        envelopes = self._ledger.read_all()
        try:
            projection = fold_events(envelopes)
        except ProjectionError as error:
            raise SchedulerStateError("persisted ledger cannot authorize activity recovery") from error
        activation = next(
            (item for item in projection.activations if item.activation_id == task.activation_id),
            None,
        )
        if (
            activation is None
            or not activation.attempts
            or activation.attempts[-1].attempt != task.attempt
            or activation.attempts[-1].activity is None
        ):
            raise SchedulerStateError(f"task activity does not match: {task.activation_id}")
        attempt = activation.attempts[-1]
        activity = attempt.activity
        assert activity is not None
        try:
            workspace = self._store.open_attempt(activity.workspace_identity)
        except Exception:
            return self._blocked_recovery(task, activity, status="indeterminate")

        if activity.state == "terminal_observed":
            self._cleanup_receipt_after_terminal(task, activity)
            self._finalize_observed_if_running(task, activity)
            return self._recovery_decision(task, activity, "promote_same_attempt", "terminal")

        receipts = self._collect_terminal_receipts(task, activity)
        if receipts is None:
            return self._blocked_recovery(task, activity, status="indeterminate")
        if len(receipts) == 1:
            return self._promote_receipt(task, activity, receipts[0])

        if activity.cancel_requested:
            cancel_status = await self._recover_cancel(task, activity, workspace)
            if cancel_status == "block":
                return self._blocked_recovery(task, activity, status="indeterminate")
            if cancel_status == "promote":
                receipts = self._collect_terminal_receipts(task, activity)
                if receipts is None or len(receipts) != 1:
                    return self._blocked_recovery(task, activity, status="indeterminate")
                return self._promote_receipt(task, activity, receipts[0])

        reconciled = await self._recover_reconcile(task, activity, workspace)
        if reconciled is None:
            return self._blocked_recovery(task, activity, status="indeterminate")

        bound = self._bind_reconcile_reference(task, activity, reconciled)
        if bound is None:
            return self._blocked_recovery(task, activity, status="indeterminate")
        activity = bound

        receipts = self._collect_terminal_receipts(task, activity)
        if receipts is None:
            return self._blocked_recovery(task, activity, status="indeterminate")
        if len(receipts) == 1:
            return self._promote_receipt(task, activity, receipts[0])

        return self._decision_after_receiptless_reconcile(task, activity, reconciled)

    async def cancel_activity(self, task: PlannedTask, *, reason: str) -> ActivityRecoveryDecision:
        """Request cancel, then map host acknowledgement without treating signals as terminal."""

        if not reason:
            raise ValueError("cancel reason must not be empty")
        activity = self._live_activity(task)
        if activity is None:
            raise SchedulerStateError(f"task activity does not match: {task.activation_id}")
        if activity.state == "terminal_observed":
            self._cleanup_receipt_after_terminal(task, activity)
            self._finalize_observed_if_running(task, activity)
            return self._recovery_decision(task, activity, "promote_same_attempt", "terminal")
        if not activity.cancel_requested:
            self._append(
                (
                    TaskActivityCancelRequested(
                        activity_id=activity.activity_id,
                        reason=reason,
                        requested_at=self._now(),
                    ),
                )
            )
            refreshed = self._live_activity(task)
            if refreshed is None:
                raise SchedulerStateError(f"task activity does not match: {task.activation_id}")
            activity = refreshed
        try:
            workspace = self._store.open_attempt(activity.workspace_identity)
        except Exception:
            return self._blocked_recovery(task, activity, status="indeterminate")
        try:
            async with asyncio.timeout(self._cancel_timeout_seconds):
                result = await self._host.cancel(self._host_cancel_call(task, activity, workspace))
        except TimeoutError:
            return self._blocked_recovery(task, activity, status="indeterminate")
        except Exception:
            return self._blocked_recovery(task, activity, status="indeterminate")
        if (
            not isinstance(result, TaskHostCallResult)
            or result.operation != "cancel"
            or result.cancel_result is None
        ):
            return self._blocked_recovery(task, activity, status="indeterminate")
        status = result.cancel_result.status
        if status == "indeterminate":
            return self._blocked_recovery(task, activity, status="indeterminate")
        if status == "terminal":
            receipts = self._collect_terminal_receipts(task, activity)
            if receipts is None or len(receipts) != 1:
                return self._blocked_recovery(task, activity, status="indeterminate")
            return self._promote_receipt(task, activity, receipts[0])
        reconciled = await self._recover_reconcile(task, activity, workspace)
        if reconciled is None:
            return self._blocked_recovery(task, activity, status="indeterminate")
        bound = self._bind_reconcile_reference(task, activity, reconciled)
        if bound is None:
            return self._blocked_recovery(task, activity, status="indeterminate")
        activity = bound
        receipts = self._collect_terminal_receipts(task, activity)
        if receipts is None:
            return self._blocked_recovery(task, activity, status="indeterminate")
        if len(receipts) == 1:
            return self._promote_receipt(task, activity, receipts[0])
        return self._decision_after_receiptless_reconcile(task, activity, reconciled)

    async def recover_live_activities(self, tasks: Sequence[PlannedTask]) -> RecoveryResult:
        decisions: list[ActivityRecoveryDecision] = []
        for task in tasks:
            if not self._task_has_live_activity(task):
                continue
            decisions.append(await self.recover_activity(task))
        return RecoveryResult(decisions=tuple(decisions))

    async def run_wave(self, tasks: Sequence[PlannedTask]) -> tuple[AttemptResult, ...]:
        selected = select_wave(tasks, self._max_parallel)
        if not selected:
            return ()
        if any(self._handler_is_recoverable(task) for task in selected):
            return await self._run_wave_including_recoverable(selected)

        leases = tuple(self._start(task) for task in selected)
        attempt_ids = tuple(self._attempt_id(task, "run") for task in selected)
        attempt_workspaces = self._store.create_attempts(attempt_ids)
        work: list[tuple[PlannedTask, AttemptWorkspace, _LeaseState]] = []
        try:
            for task, lease, attempt_workspace in zip(
                selected,
                leases,
                attempt_workspaces,
                strict=True,
            ):
                work.append((task, attempt_workspace, _LeaseState(self, lease)))
        except BaseException:
            for workspace in attempt_workspaces:
                workspace.discard()
            raise

        gathered = await asyncio.gather(
            *(self._execute(task, workspace, lease_state) for task, workspace, lease_state in work)
        )
        finalized: list[AttemptResult] = []
        for result in gathered:
            finalized.append(self._finalize(result))
        return tuple(finalized)

    async def _run_wave_including_recoverable(
        self, selected: Sequence[PlannedTask]
    ) -> tuple[AttemptResult, ...]:
        work: list[tuple[PlannedTask, AttemptWorkspace, _LeaseState, str | None]] = []
        try:
            for task in selected:
                if self._handler_is_recoverable(task):
                    lease, workspace, _identity = self.start_recoverable(task)
                    activity_id = activity_id_for_attempt(
                        task.invocation_id,
                        task.task_id,
                        task.activation_id,
                        task.attempt,
                    )
                    self.task_activity_port(
                        TaskActivityRpcIdentity(
                            invocation_id=task.invocation_id,
                            task_id=task.task_id,
                            activation_id=task.activation_id,
                            attempt=task.attempt,
                            activity_id=activity_id,
                        )
                    )
                else:
                    lease = self._start(task)
                    workspace = self._store.create_attempt(self._attempt_id(task, "run"))
                    activity_id = None
                work.append((task, workspace, _LeaseState(self, lease), activity_id))
        except BaseException:
            for _task, workspace, _lease, activity_id in work:
                if activity_id is None:
                    workspace.discard()
            raise

        gathered = await asyncio.gather(
            *(
                self._execute(task, workspace, lease_state, activity_id=activity_id)
                for task, workspace, lease_state, activity_id in work
            )
        )
        return tuple(self._finalize_wave_result(result) for result in gathered)

    async def resume_running(self, tasks: Sequence[PlannedTask]) -> tuple[AttemptResult, ...]:
        """Resume attempts whose start and deterministic lease are already authoritative."""
        selected = select_wave(tasks, self._max_parallel)
        if not selected:
            return ()
        envelopes = self._ledger.read_all()
        running = self._persisted_running_leases(envelopes)
        work: list[tuple[PlannedTask, AttemptWorkspace, _LeaseState, str | None]] = []
        try:
            for task in selected:
                _validate_running_transition(task, envelopes)
                if (
                    self._task_has_live_activity(task)
                    and (task.task_id, task.attempt) not in self._same_attempt_execute
                ):
                    continue
                lease = running.get((task.task_id, task.attempt))
                if lease is None or lease.owner_id != self._owner_id:
                    raise LeaseUnavailableError(
                        "persisted running task is not owned by this deterministic scheduler"
                    )
                if lease.expires_at < self._now():
                    raise LeaseUnavailableError("persisted running task lease expired")
                if self._task_has_live_activity(task):
                    activity = self._live_activity(task)
                    if activity is None:
                        raise TaskActivityRecoveryUnsupported(
                            "recoverable attempt recovery cannot recreate the workspace"
                        )
                    workspace = self._store.open_attempt(activity.workspace_identity)
                    work.append(
                        (
                            task,
                            workspace,
                            _LeaseState(self, lease),
                            activity.activity_id,
                        )
                    )
                    self._same_attempt_execute.discard((task.task_id, task.attempt))
                    continue
                workspace = self._store.reset_attempt(self._attempt_id(task, "run"))
                work.append((task, workspace, _LeaseState(self, lease), None))
        except BaseException:
            for _task, workspace, _lease, activity_id in work:
                if activity_id is None:
                    workspace.discard()
            raise
        if not work:
            return ()
        gathered = await asyncio.gather(
            *(
                self._execute(task, workspace, lease_state, activity_id=activity_id)
                for task, workspace, lease_state, activity_id in work
            )
        )
        return tuple(self._finalize_wave_result(result) for result in gathered)

    def heartbeat(self, lease: Lease) -> Lease:
        guard = self._lease_guard(lease)
        persisted = guard.running
        if persisted != lease:
            raise ValueError("heartbeat lease does not match persisted running state")
        now = self._now()
        if now < lease.heartbeat_at:
            raise ValueError("clock moved backwards before lease heartbeat")
        if now > lease.expires_at:
            raise ValueError("expired lease cannot be renewed")
        updated = lease.model_copy(update={"heartbeat_at": now, "expires_at": now + self._lease_seconds})
        self._append(
            (
                TaskLeaseHeartbeat(
                    task_id=updated.task_id,
                    activation_id=updated.activation_id,
                    attempt=updated.attempt,
                    owner_id=updated.owner_id,
                    heartbeat_at=updated.heartbeat_at,
                    expires_at=updated.expires_at,
                ),
            ),
            expected_next_seq=guard.expected_next_seq,
        )
        return updated

    def reclaim_expired(self, leases: Sequence[Lease] | None = None) -> tuple[str, ...]:
        envelopes = self._ledger.read_all()
        persisted = self._persisted_running_leases(envelopes)
        if leases is not None:
            requested = {(item.task_id, item.activation_id, item.attempt) for item in leases}
            persisted = {
                key: lease
                for key, lease in persisted.items()
                if (lease.task_id, lease.activation_id, lease.attempt) in requested
            }
        now = self._now()
        expired = tuple(
            sorted(
                (lease for lease in persisted.values() if lease.expires_at < now),
                key=lambda item: (item.task_id, item.attempt, item.activation_id),
            )
        )
        try:
            projection = fold_events(tuple(envelopes))
        except ProjectionError as error:
            raise SchedulerStateError("persisted ledger cannot authorize lease reclamation") from error
        reclaimable = tuple(lease for lease in expired if not _lease_has_live_activity(projection, lease))
        if reclaimable:
            failure = TaskOutcome.failed("transient", "persisted task lease expired").failure
            assert failure is not None
            self._append(
                tuple(
                    TaskAttemptFailed(
                        activation_id=lease.activation_id,
                        attempt=lease.attempt,
                        failure=failure,
                    )
                    for lease in reclaimable
                ),
                expected_next_seq=_next_sequence(envelopes),
            )
        return tuple(lease.task_id for lease in reclaimable)

    def _start(self, task: PlannedTask) -> Lease:
        envelopes = self._ledger.read_all()
        _validate_start_transition(task, envelopes)
        acquired = self._now()
        lease = Lease(
            task_id=task.task_id,
            activation_id=task.activation_id,
            attempt=task.attempt,
            owner_id=self._owner_id,
            acquired_at=acquired,
            heartbeat_at=acquired,
            expires_at=acquired + self._lease_seconds,
        )
        events: tuple[RuntimeEvent, ...] = (
            TaskAttemptStarted(
                activation_id=task.activation_id,
                attempt=task.attempt,
                lease_expires_at=format(lease.expires_at, ".17g"),
            ),
            TaskLeaseAcquired(**lease.model_dump()),
        )
        expected_next_seq = _next_sequence(envelopes)
        self._append(events, expected_next_seq=expected_next_seq)
        return lease

    async def _execute(
        self,
        task: PlannedTask,
        workspace: AttemptWorkspace,
        lease_state: _LeaseState,
        *,
        activity_id: str | None = None,
    ) -> AttemptResult:
        try:
            if self._registry.task_handlers.get(task.capability_id) is None:
                return AttemptResult(
                    task=task,
                    outcome=TaskOutcome.failed("internal", f"missing task handler: {task.capability_id}"),
                    lease=lease_state.current,
                )
            request = self._project_request(task)

            try:
                async with asyncio.timeout(task.timeout_seconds):
                    result = await self._host.execute(
                        self._host_execute_call(task, request, workspace, activity_id=activity_id)
                    )
            except TimeoutError:
                if activity_id is not None:
                    await self.cancel_activity(task, reason="timeout")
                    activity = self._live_activity(task)
                    outcome = (
                        activity.terminal
                        if activity is not None and activity.terminal is not None
                        else TaskOutcome.failed("timeout", "task handler exceeded its run timeout")
                    )
                    return AttemptResult(task=task, outcome=outcome, lease=lease_state.current)
                outcome = TaskOutcome.failed("timeout", "task handler exceeded its run timeout")
            except asyncio.CancelledError as error:
                if activity_id is not None:
                    raise
                outcome = TaskOutcome.failed("internal", _exception_message("task handler raised", error))
            except Exception as error:
                if activity_id is not None:
                    raise
                outcome = TaskOutcome.failed("internal", _exception_message("task handler raised", error))
            else:
                if (
                    not isinstance(result, TaskHostCallResult)
                    or result.operation != "execute"
                    or result.outcome is None
                ):
                    outcome = TaskOutcome.failed(
                        "internal",
                        f"task host returned {type(result).__name__}, expected execute TaskHostCallResult",
                    )
                else:
                    outcome = result.outcome
            if not isinstance(outcome, TaskOutcome):
                outcome = TaskOutcome.failed(
                    "internal",
                    f"task handler returned {type(outcome).__name__}, expected TaskOutcome",
                )
            if activity_id is not None:
                activity = self._live_activity(task)
                if activity is not None:
                    receipts = self._collect_terminal_receipts(task, activity)
                    if receipts is None:
                        return AttemptResult(task=task, outcome=outcome, lease=lease_state.current)
                    if len(receipts) == 1:
                        self._promote_receipt(task, activity, receipts[0])
                        return AttemptResult(
                            task=task,
                            outcome=receipts[0].outcome,
                            lease=lease_state.current,
                        )
                return AttemptResult(task=task, outcome=outcome, lease=lease_state.current)
            if outcome.status != "succeeded":
                return AttemptResult(task=task, outcome=outcome, lease=lease_state.current)
            try:
                candidate = workspace.seal()
            except Exception as error:
                return AttemptResult(
                    task=task,
                    outcome=TaskOutcome.failed(
                        "internal", _exception_message("candidate sealing failed", error)
                    ),
                    lease=lease_state.current,
                )
            return AttemptResult(
                task=task,
                outcome=outcome,
                lease=lease_state.current,
                candidate=candidate,
            )
        finally:
            if activity_id is None:
                workspace.discard()

    def _finalize(self, result: AttemptResult, *, allow_expired_lease: bool = False) -> AttemptResult:
        task = result.task
        guard = self._lease_guard(result.lease)
        if not _same_lease_owner(guard.running, result.lease):
            return _replace_with_lease_failure(result, "task lease is no longer the persisted running lease")
        assert guard.running is not None
        if not allow_expired_lease and guard.running.expires_at < self._now():
            expired = _replace_with_lease_failure(result, "persisted task lease expired")
            assert expired.outcome.failure is not None
            self._append(
                (
                    TaskAttemptFailed(
                        activation_id=task.activation_id,
                        attempt=task.attempt,
                        failure=expired.outcome.failure,
                    ),
                ),
                expected_next_seq=guard.expected_next_seq,
            )
            return expired
        if result.outcome.status == "failed":
            assert result.outcome.failure is not None
            self._append(
                (
                    TaskAttemptFailed(
                        activation_id=task.activation_id,
                        attempt=task.attempt,
                        failure=result.outcome.failure,
                    ),
                ),
                expected_next_seq=guard.expected_next_seq,
            )
            return result
        if result.outcome.status == "stopped":
            assert result.outcome.stop_reason is not None
            self._append(
                (
                    TaskAttemptStopped(
                        activation_id=task.activation_id,
                        attempt=task.attempt,
                        reason=result.outcome.stop_reason,
                        output=result.outcome.output,
                    ),
                ),
                expected_next_seq=guard.expected_next_seq,
            )
            return result

        candidate = result.candidate
        if candidate is None:
            return self._record_commit_failure(
                result,
                "successful handler produced no candidate",
                expected_next_seq=guard.expected_next_seq,
            )
        try:
            prepared_intents = self._validated_effect_intents(task, result.outcome)
        except _InvalidPreparedOutput as error:
            return self._record_commit_failure(
                result,
                str(error),
                kind="invalid_output",
                expected_next_seq=guard.expected_next_seq,
            )
        try:
            current_tree_id = self._store.head_tree_id()
            if candidate.baseline_tree_id != current_tree_id:
                candidate = self._store.rebase_candidate(candidate, self._attempt_id(task, "rebase"))
            events = self.prepare_success(
                task,
                result.outcome,
                candidate.baseline_tree_id,
                candidate.candidate_tree_id,
                prepared_intents,
            )
            range_digest = _prepared_event_range_digest(guard.expected_next_seq, events)
            validators = tuple(
                (validator_id, self._registry.commit_validators[validator_id])
                for validator_id in task.validators
            )
            context = ValidationContext(
                invocation_id=task.invocation_id,
                task_id=task.task_id,
                graph_instance_id=task.graph_instance_id,
                node_id=task.node_id,
                resources=task.resources,
            )

            def authorize_publish() -> None:
                self._guard_transition()
                self._require_live_lease(result.lease, allow_expired=allow_expired_lease)

            def publish_prepared(_previous_tree_id: str, _tree_id: str) -> None:
                live = self._require_live_lease(result.lease, allow_expired=allow_expired_lease)
                self._append_prepared(events, expected_next_seq=live.expected_next_seq)

            commit = self._store.finalize_candidate(
                candidate,
                task.resources,
                validators,
                context,
                authorize_publish=authorize_publish,
                publish_prepared=publish_prepared,
                prepared_event_range_digest=range_digest,
            )
        except LeaseUnavailableError:
            expired = _replace_with_lease_failure(result, "persisted task lease expired")
            fresh = self._lease_guard(result.lease)
            if _same_lease_owner(fresh.running, result.lease):
                assert fresh.running is not None
                if fresh.running.expires_at >= self._now():
                    raise SchedulerStateError("live lease authorization failed inconsistently")
                assert expired.outcome.failure is not None
                self._append(
                    (
                        TaskAttemptFailed(
                            activation_id=task.activation_id,
                            attempt=task.attempt,
                            failure=expired.outcome.failure,
                        ),
                    ),
                    expected_next_seq=fresh.expected_next_seq,
                )
            return expired
        except (FinalizationRolledBack, HeadPublicationIndeterminate):
            raise
        except Exception as error:
            fresh = self._lease_guard(result.lease)
            if not _same_lease_owner(fresh.running, result.lease):
                return _replace_with_lease_failure(
                    result, "task lease ended while candidate finalization failed"
                )
            return self._record_commit_failure(
                result,
                _exception_message("candidate commit failed", error),
                expected_next_seq=fresh.expected_next_seq,
            )
        if not commit.committed:
            reasons = "; ".join(
                f"{receipt.validator_id}: {receipt.reason}"
                for receipt in commit.receipts
                if not receipt.accepted
            )
            fresh = self._lease_guard(result.lease)
            if not _same_lease_owner(fresh.running, result.lease):
                return _replace_with_lease_failure(
                    result, "task lease ended after commit validation rejection"
                )
            return self._record_commit_failure(
                result,
                f"commit validation rejected: {reasons}",
                kind="invalid_output",
                commit=commit,
                expected_next_seq=fresh.expected_next_seq,
            )
        return result.model_copy(
            update={
                "candidate": candidate,
                "commit": commit,
                "head_tree_id": candidate.candidate_tree_id,
                "effect_ids": tuple(effect_id for effect_id, _intent, _key in prepared_intents),
            }
        )

    def _record_commit_failure(
        self,
        result: AttemptResult,
        message: str,
        *,
        kind: FailureKind = "internal",
        commit: CommitResult | None = None,
        expected_next_seq: int,
    ) -> AttemptResult:
        outcome = TaskOutcome.failed(kind, message)
        assert outcome.failure is not None
        self._append(
            (
                TaskAttemptFailed(
                    activation_id=result.task.activation_id,
                    attempt=result.task.attempt,
                    failure=outcome.failure,
                ),
            ),
            expected_next_seq=expected_next_seq,
        )
        return result.model_copy(update={"outcome": outcome, "commit": commit})

    def prepare_success(
        self,
        task: PlannedTask,
        outcome: TaskOutcome,
        previous_tree_id: str,
        tree_id: str,
        intents: tuple[tuple[str, EffectIntent, str], ...] | None = None,
    ) -> tuple[RuntimeEvent, ...]:
        prepared_intents = intents if intents is not None else self._validated_effect_intents(task, outcome)
        effect_ids = tuple(effect_id for effect_id, _intent, _key in prepared_intents)
        events: list[RuntimeEvent] = [
            TaskCommitPrepared(
                task_id=task.task_id,
                activation_id=task.activation_id,
                attempt=task.attempt,
                output=outcome.output,
                previous_tree_id=previous_tree_id,
                tree_id=tree_id,
                effect_ids=effect_ids,
            ),
            HeadAdvanced(
                task_id=task.task_id,
                activation_id=task.activation_id,
                attempt=task.attempt,
                previous_tree_id=previous_tree_id,
                tree_id=tree_id,
            ),
        ]
        if prepared_intents:
            events.extend(
                EffectIntentCommitted(
                    effect_id=effect_id,
                    activation_id=task.activation_id,
                    attempt=task.attempt,
                    index=index,
                    effect_kind=intent.kind,
                    payload=intent.payload,
                    idempotency_key=key,
                )
                for index, (effect_id, intent, key) in enumerate(prepared_intents)
            )
        else:
            events.append(
                TaskAttemptSucceeded(
                    activation_id=task.activation_id,
                    attempt=task.attempt,
                    output=outcome.output,
                )
            )
        return tuple(events)

    def _validated_effect_intents(
        self,
        task: PlannedTask,
        outcome: TaskOutcome,
    ) -> tuple[tuple[str, EffectIntent, str], ...]:
        prepared: list[tuple[str, EffectIntent, str]] = []
        lock_digest = self._resolved_lock_digest()
        for index, intent in enumerate(outcome.effects):
            try:
                entry = self._effects.require(intent.kind)
            except KeyError as error:
                raise _InvalidPreparedOutput(f"unknown effect kind: {intent.kind}") from error
            schema = self._schemas.entries.get(entry.intent_schema_id)
            if schema is None:
                raise _InvalidPreparedOutput(
                    f"effect intent schema is not registered: {entry.intent_schema_id}"
                )
            try:
                _validate_json_schema(thaw_json(intent.payload), schema.content)
            except ValueError as error:
                raise _InvalidPreparedOutput(
                    f"effect intent payload is invalid for {intent.kind}: {error}"
                ) from error
            effect_id = canonical_id(
                "effect",
                task.invocation_id,
                task.activation_id,
                str(task.attempt),
                str(index),
            )
            payload = thaw_json(intent.payload)
            idempotency_key = canonical_digest(
                {
                    "lock_digest": lock_digest,
                    "effect_id": effect_id,
                    "kind": intent.kind,
                    "payload_digest": canonical_digest(payload),
                }
            )
            prepared.append((effect_id, intent, idempotency_key))
        return tuple(prepared)

    def _resolved_lock_digest(self) -> str:
        if self._lock_digest is not None:
            return self._lock_digest
        projection = fold_events(self._ledger.read_all())
        if projection.lock_digest is None:
            raise SchedulerStateError("invocation lock digest is unavailable")
        return projection.lock_digest

    def _resolved_composition_digest(self) -> str:
        if self._composition_digest is not None:
            return self._composition_digest
        return canonical_digest({"lock_digest": self._resolved_lock_digest()})

    def _resolved_entrypoint(self) -> str:
        if self._entrypoint is not None:
            return self._entrypoint
        projection = fold_events(self._ledger.read_all())
        if not projection.entrypoint:
            raise SchedulerStateError("invocation entrypoint is unavailable")
        return projection.entrypoint

    def _project_request(self, task: PlannedTask) -> TaskRequest:
        binding = getattr(self._registry, "bindings", {}).get(task.capability_id)
        if binding is not None:
            target_capability_id = binding.target_capability_id
            binding_data = thaw_json(binding.data)
            resource_ids = tuple(sorted(binding.resource_ids))
        else:
            target_capability_id = None
            binding_data = None
            resource_ids = ()
        resource_digests: dict[str, str] = {}
        if resource_ids:
            entries = getattr(self._resources, "entries", {})
            for resource_id in resource_ids:
                try:
                    resource_digests[resource_id] = entries[resource_id].sha256
                except KeyError as error:
                    raise SchedulerStateError(f"binding resource is not registered: {resource_id}") from error
        return TaskRequest(
            invocation_id=task.invocation_id,
            task_id=task.task_id,
            graph_instance_id=task.graph_instance_id,
            node_id=task.node_id,
            capability_id=task.capability_id,
            target_capability_id=target_capability_id,
            binding_data=binding_data,
            resource_ids=resource_ids,
            resource_digests=resource_digests,
            resources=task.resources,
            invocation=InvocationMetadata(
                invocation_id=task.invocation_id,
                lock_digest=self._resolved_lock_digest(),
                composition_digest=self._resolved_composition_digest(),
                entrypoint=self._resolved_entrypoint(),
            ),
            attempt=task.attempt,
            input=thaw_json(task.input),
            prior_failure=task.prior_failure,
        )

    def _host_execute_call(
        self,
        task: PlannedTask,
        request: TaskRequest,
        workspace: AttemptWorkspace,
        *,
        activity_id: str | None = None,
    ) -> TaskHostExecuteCall:
        binding = getattr(self._registry, "bindings", {}).get(task.capability_id)
        capability_id = binding.target_capability_id if binding is not None else task.capability_id
        host_lock = pinned_execution_host_lock()
        return TaskHostExecuteCall(
            identity=TaskHostCallIdentity(
                invocation_id=task.invocation_id,
                task_id=task.task_id,
                activation_id=task.activation_id,
                attempt=task.attempt,
                activity_id=activity_id,
                operation="execute",
                host_implementation_id=host_lock.implementation_id,
                host_implementation_digest=host_lock.implementation_digest,
                wire_schema_version=host_lock.wire_schema_version,
            ),
            capability_id=capability_id,
            capability_entrypoint=self._capability_entrypoint(capability_id),
            request=request,
            attempt_root=AttemptRootDescriptor(attempt_directory_id=workspace.attempt_id),
            activity_rpc=TaskActivityRpcIdentity(
                invocation_id=task.invocation_id,
                task_id=task.task_id,
                activation_id=task.activation_id,
                attempt=task.attempt,
                activity_id=activity_id,
            ),
            authorized_secret_handles=(),
        )

    def _host_call_identity(
        self,
        task: PlannedTask,
        activity_id: str,
        operation: HostOperation,
    ) -> TaskHostCallIdentity:
        host_lock = pinned_execution_host_lock()
        return TaskHostCallIdentity(
            invocation_id=task.invocation_id,
            task_id=task.task_id,
            activation_id=task.activation_id,
            attempt=task.attempt,
            activity_id=activity_id,
            operation=operation,
            host_implementation_id=host_lock.implementation_id,
            host_implementation_digest=host_lock.implementation_digest,
            wire_schema_version=host_lock.wire_schema_version,
        )

    def _host_lifecycle_call_fields(
        self,
        task: PlannedTask,
        activity: TaskActivitySnapshot,
        workspace: AttemptWorkspace,
        operation: HostOperation,
    ) -> dict[str, object]:
        binding = getattr(self._registry, "bindings", {}).get(task.capability_id)
        capability_id = binding.target_capability_id if binding is not None else task.capability_id
        return {
            "identity": self._host_call_identity(task, activity.activity_id, operation),
            "capability_id": capability_id,
            "capability_entrypoint": self._capability_entrypoint(capability_id),
            "request": self._project_request(task),
            "attempt_root": AttemptRootDescriptor(attempt_directory_id=workspace.attempt_id),
            "activity_rpc": TaskActivityRpcIdentity(
                invocation_id=task.invocation_id,
                task_id=task.task_id,
                activation_id=task.activation_id,
                attempt=task.attempt,
                activity_id=activity.activity_id,
            ),
            "authorized_secret_handles": (),
            "activity": activity,
        }

    def _host_reconcile_call(
        self,
        task: PlannedTask,
        activity: TaskActivitySnapshot,
        workspace: AttemptWorkspace,
    ) -> TaskHostReconcileCall:
        return TaskHostReconcileCall(
            **self._host_lifecycle_call_fields(task, activity, workspace, "reconcile")  # type: ignore[arg-type]
        )

    def _host_cancel_call(
        self,
        task: PlannedTask,
        activity: TaskActivitySnapshot,
        workspace: AttemptWorkspace,
    ) -> TaskHostCancelCall:
        return TaskHostCancelCall(
            **self._host_lifecycle_call_fields(task, activity, workspace, "cancel")  # type: ignore[arg-type]
        )

    def _collect_terminal_receipts(
        self,
        task: PlannedTask,
        activity: TaskActivitySnapshot,
    ) -> tuple[TaskHostTerminalReceipt, ...] | None:
        collected: list[TaskHostTerminalReceipt] = []
        for operation in ("execute", "reconcile", "cancel"):
            identity = self._host_call_identity(task, activity.activity_id, operation)
            try:
                if self._receipts is not None:
                    found = self._receipts.authenticate(identity)
                else:
                    found = self._host.read_terminal_receipts(identity)
            except Exception:
                return None
            if not isinstance(found, tuple):
                return None
            for item in found:
                if not isinstance(item, TaskHostTerminalReceipt):
                    return None
                collected.append(item)
        if len(collected) > 1:
            return None
        return tuple(collected)

    async def _recover_cancel(
        self,
        task: PlannedTask,
        activity: TaskActivitySnapshot,
        workspace: AttemptWorkspace,
    ) -> str:
        try:
            async with asyncio.timeout(self._cancel_timeout_seconds):
                result = await self._host.cancel(self._host_cancel_call(task, activity, workspace))
        except TimeoutError:
            return "block"
        except Exception:
            return "block"
        if (
            not isinstance(result, TaskHostCallResult)
            or result.operation != "cancel"
            or result.cancel_result is None
        ):
            return "block"
        status = result.cancel_result.status
        if status == "terminal":
            return "promote"
        if status == "acknowledged":
            return "continue"
        return "block"

    def _promote_receipt(
        self,
        task: PlannedTask,
        activity: TaskActivitySnapshot,
        receipt: TaskHostTerminalReceipt,
    ) -> ActivityRecoveryDecision:
        if not _receipt_matches_activity(receipt, task, activity):
            return self._blocked_recovery(task, activity, status="indeterminate")
        if activity.state == "terminal_observed":
            self._cleanup_receipt_after_terminal(task, activity)
            self._finalize_observed_if_running(task, activity)
            return self._recovery_decision(task, activity, "promote_same_attempt", "terminal")
        candidate_tree_id: str | None = None
        digest: str | None = None
        sealed_candidate: CandidateWriteSet | None = None
        if receipt.outcome.status == "succeeded":
            try:
                sealed_candidate = self._store.seal_authenticated_candidate(
                    activity.workspace_identity,
                    task.resources,
                    self._named_validators(task),
                    self._validation_context(task),
                )
            except Exception:
                return self._blocked_recovery(task, activity, status="indeterminate")
            candidate_tree_id = sealed_candidate.candidate_tree_id
            digest = write_set_digest(sealed_candidate)
            _promotion_cut("after_candidate_seal")
        event = TaskActivityTerminalObserved(
            activity_id=activity.activity_id,
            outcome=receipt.outcome,
            outcome_digest=receipt.outcome_digest,
            terminal_proof_digest=receipt.terminal_proof_digest,
            candidate_tree_id=candidate_tree_id,
            write_set_digest=digest,
        )
        global _PROMOTING_TERMINAL
        _PROMOTING_TERMINAL = True
        try:
            self._append((event,))
        finally:
            _PROMOTING_TERMINAL = False
        try:
            self._ledger.ensure_durable()
        except Exception:
            observed = self._live_activity(task)
            if observed is None or observed.state != "terminal_observed":
                return self._blocked_recovery(task, activity, status="indeterminate")
        _promotion_cut("before_receipt_cleanup")
        observed = self._live_activity(task)
        if observed is None:
            return self._blocked_recovery(task, activity, status="indeterminate")
        self._delete_promoted_receipt(task, receipt, event)
        self._finalize_observed_if_running(task, observed, candidate=sealed_candidate)
        return self._recovery_decision(task, observed, "promote_same_attempt", "terminal")

    def _delete_promoted_receipt(
        self,
        task: PlannedTask,
        receipt: TaskHostTerminalReceipt,
        terminal: TaskActivityTerminalObserved,
    ) -> None:
        if self._receipts is None:
            return
        identity = self._host_call_identity(task, receipt.activity_id, receipt.operation)
        try:
            self._receipts.delete_authenticated(identity, receipt, terminal)
        except TerminalReceiptError:
            return

    def _cleanup_receipt_after_terminal(self, task: PlannedTask, activity: TaskActivitySnapshot) -> None:
        if (
            self._receipts is None
            or activity.terminal is None
            or activity.outcome_digest is None
            or activity.state != "terminal_observed"
        ):
            return
        terminal = TaskActivityTerminalObserved(
            activity_id=activity.activity_id,
            outcome=activity.terminal,
            outcome_digest=activity.outcome_digest,
            terminal_proof_digest=activity.terminal_proof_digest,
            candidate_tree_id=activity.candidate_tree_id,
            write_set_digest=activity.write_set_digest,
        )
        for operation in ("execute", "reconcile", "cancel"):
            identity = self._host_call_identity(task, activity.activity_id, operation)
            try:
                found = self._receipts.authenticate(identity)
            except TerminalReceiptError:
                continue
            if len(found) != 1:
                continue
            try:
                self._receipts.delete_authenticated(identity, found[0], terminal)
            except TerminalReceiptError:
                continue

    def _named_validators(self, task: PlannedTask) -> tuple[tuple[str, CommitValidator], ...]:
        return tuple(
            (validator_id, self._registry.commit_validators[validator_id]) for validator_id in task.validators
        )

    def _validation_context(self, task: PlannedTask) -> ValidationContext:
        return ValidationContext(
            invocation_id=task.invocation_id,
            task_id=task.task_id,
            graph_instance_id=task.graph_instance_id,
            node_id=task.node_id,
            resources=task.resources,
        )

    async def _recover_reconcile(
        self,
        task: PlannedTask,
        activity: TaskActivitySnapshot,
        workspace: AttemptWorkspace,
    ) -> TaskActivityReconcileResult | None:
        try:
            result = await self._host.reconcile(self._host_reconcile_call(task, activity, workspace))
        except Exception:
            return None
        if (
            not isinstance(result, TaskHostCallResult)
            or result.operation != "reconcile"
            or result.reconcile_result is None
        ):
            return None
        return result.reconcile_result

    def _bind_reconcile_reference(
        self,
        task: PlannedTask,
        activity: TaskActivitySnapshot,
        reconciled: TaskActivityReconcileResult,
    ) -> TaskActivitySnapshot | None:
        if reconciled.reference is None:
            return activity
        try:
            port = self.task_activity_port(
                TaskActivityRpcIdentity(
                    invocation_id=task.invocation_id,
                    task_id=task.task_id,
                    activation_id=task.activation_id,
                    attempt=task.attempt,
                    activity_id=activity.activity_id,
                )
            )
            return port.bind(thaw_json(reconciled.reference))
        except Exception:
            return None

    def _adopt_recovered_lease(
        self,
        task: PlannedTask,
        activity: TaskActivitySnapshot,
        reconciled: TaskActivityReconcileResult,
    ) -> None:
        now = self._now()
        evidence = canonical_digest(cast(JSONValue, reconciled.model_dump(mode="json")))
        self._append(
            (
                TaskLeaseAdopted(
                    activity_id=activity.activity_id,
                    task_id=task.task_id,
                    activation_id=task.activation_id,
                    attempt=task.attempt,
                    owner_id=self._owner_id,
                    acquired_at=now,
                    heartbeat_at=now,
                    expires_at=now + self._lease_seconds,
                    reconciliation_evidence_digest=evidence,
                ),
            )
        )

    def _recovery_decision(
        self,
        task: PlannedTask,
        activity: TaskActivitySnapshot,
        decision: str,
        status: str | None,
    ) -> ActivityRecoveryDecision:
        return ActivityRecoveryDecision(
            activity_id=activity.activity_id,
            task_id=task.task_id,
            activation_id=task.activation_id,
            attempt=task.attempt,
            decision=decision,  # type: ignore[arg-type]
            reconcile_status=status,  # type: ignore[arg-type]
        )

    def _blocked_recovery(
        self,
        task: PlannedTask,
        activity: TaskActivitySnapshot,
        *,
        status: str,
    ) -> ActivityRecoveryDecision:
        return self._recovery_decision(task, activity, "block", status)

    def _decision_after_receiptless_reconcile(
        self,
        task: PlannedTask,
        activity: TaskActivitySnapshot,
        reconciled: TaskActivityReconcileResult,
    ) -> ActivityRecoveryDecision:
        decision = recovery_decision_for_status(reconciled.status)
        if decision in {"promote_same_attempt", "finalize_failure_then_retry_policy"}:
            return self._blocked_recovery(task, activity, status="indeterminate")
        if decision in {"execute_same_attempt", "adopt_same_attempt"}:
            self._adopt_recovered_lease(task, activity, reconciled)
            if decision == "execute_same_attempt":
                self._same_attempt_execute.add((task.task_id, task.attempt))
        return self._recovery_decision(task, activity, decision, reconciled.status)

    def _task_has_live_activity(self, task: PlannedTask) -> bool:
        record = self._latest_attempt_record(task)
        return record is not None and record.activity is not None and record.status == "running"

    def _live_activity(self, task: PlannedTask) -> TaskActivitySnapshot | None:
        record = self._latest_attempt_record(task)
        if record is None:
            return None
        return record.activity

    def _latest_attempt_record(self, task: PlannedTask) -> AttemptRecord | None:
        try:
            projection = fold_events(self._ledger.read_all())
        except ProjectionError:
            return None
        activation = next(
            (item for item in projection.activations if item.activation_id == task.activation_id),
            None,
        )
        if activation is None or not activation.attempts or activation.attempts[-1].attempt != task.attempt:
            return None
        return activation.attempts[-1]

    def _attempt_is_running(self, task: PlannedTask) -> bool:
        record = self._latest_attempt_record(task)
        return record is not None and record.status == "running"

    def _candidate_from_observed(
        self,
        task: PlannedTask,
        activity: TaskActivitySnapshot,
    ) -> CandidateWriteSet | None:
        del task
        if activity.terminal is None or activity.terminal.status != "succeeded":
            return None
        if activity.candidate_tree_id is None or activity.write_set_digest is None:
            return None
        try:
            candidate = self._store.open_recorded_candidate(
                activity.workspace_identity,
                activity.candidate_tree_id,
            )
        except Exception:
            return None
        if write_set_digest(candidate) != activity.write_set_digest:
            return None
        return candidate

    def _finalize_observed_if_running(
        self,
        task: PlannedTask,
        activity: TaskActivitySnapshot,
        *,
        candidate: CandidateWriteSet | None = None,
    ) -> AttemptResult | None:
        if activity.state != "terminal_observed" or activity.terminal is None:
            return None
        if not self._attempt_is_running(task):
            return None
        lease = self._persisted_running_leases().get((task.task_id, task.attempt))
        if lease is None:
            return None
        sealed = candidate
        if activity.terminal.status == "succeeded" and sealed is None:
            sealed = self._candidate_from_observed(task, activity)
        return self._finalize(
            AttemptResult(
                task=task,
                outcome=activity.terminal,
                lease=lease,
                candidate=sealed,
            ),
            allow_expired_lease=True,
        )

    def _finalize_wave_result(self, result: AttemptResult) -> AttemptResult:
        if not self._handler_is_recoverable(result.task):
            return self._finalize(result)
        activity = self._live_activity(result.task)
        if activity is None or activity.state != "terminal_observed" or activity.terminal is None:
            return result
        if not self._attempt_is_running(result.task):
            return result
        candidate = result.candidate
        if activity.terminal.status == "succeeded" and candidate is None:
            candidate = self._candidate_from_observed(result.task, activity)
        return self._finalize(
            result.model_copy(update={"outcome": activity.terminal, "candidate": candidate})
        )

    def _handler_is_recoverable(self, task: PlannedTask) -> bool:
        handler = self._registry.task_handlers.get(task.capability_id)
        return isinstance(handler, RecoverableTaskHandler)

    def _capability_entrypoint(self, capability_id: str) -> str:
        entries = getattr(self._registry, "entries", {})
        entry = entries.get(capability_id)
        provenance = getattr(entry, "provenance", None) or getattr(entry, "target_provenance", None)
        callable_path = getattr(provenance, "callable_path", None)
        if isinstance(callable_path, str) and callable_path:
            return callable_path
        return capability_id

    def _lease_guard(self, lease: Lease) -> _LeaseGuard:
        envelopes = self._ledger.read_all()
        running = self._persisted_running_leases(envelopes).get((lease.task_id, lease.attempt))
        return _LeaseGuard(expected_next_seq=_next_sequence(envelopes), running=running)

    def _require_live_lease(self, lease: Lease, *, allow_expired: bool = False) -> _LeaseGuard:
        guard = self._lease_guard(lease)
        if not _same_lease_owner(guard.running, lease):
            raise LeaseUnavailableError("task no longer owns the persisted running lease")
        assert guard.running is not None
        if not allow_expired and guard.running.expires_at < self._now():
            raise LeaseUnavailableError("persisted task lease expired")
        return guard

    def _persisted_running_leases(
        self, envelopes: Sequence[EventEnvelope] | None = None
    ) -> dict[tuple[str, int], Lease]:
        running: dict[tuple[str, int], Lease] = {}
        by_activation: dict[tuple[str, int], tuple[str, int]] = {}
        for envelope in self._ledger.read_all() if envelopes is None else envelopes:
            event = envelope.event
            if isinstance(event, TaskLeaseAcquired):
                key = (event.task_id, event.attempt)
                running[key] = Lease(
                    task_id=event.task_id,
                    activation_id=event.activation_id,
                    attempt=event.attempt,
                    owner_id=event.owner_id,
                    acquired_at=event.acquired_at,
                    heartbeat_at=event.heartbeat_at,
                    expires_at=event.expires_at,
                )
                by_activation[(event.activation_id, event.attempt)] = key
            elif isinstance(event, TaskLeaseAdopted):
                key = (event.task_id, event.attempt)
                running[key] = Lease(
                    task_id=event.task_id,
                    activation_id=event.activation_id,
                    attempt=event.attempt,
                    owner_id=event.owner_id,
                    acquired_at=event.acquired_at,
                    heartbeat_at=event.heartbeat_at,
                    expires_at=event.expires_at,
                )
                by_activation[(event.activation_id, event.attempt)] = key
            elif isinstance(event, TaskLeaseHeartbeat):
                key = (event.task_id, event.attempt)
                lease = running.get(key)
                if (
                    lease is not None
                    and lease.owner_id == event.owner_id
                    and lease.heartbeat_at <= event.heartbeat_at <= lease.expires_at
                    and event.heartbeat_at <= event.expires_at
                ):
                    running[key] = lease.model_copy(
                        update={
                            "heartbeat_at": event.heartbeat_at,
                            "expires_at": event.expires_at,
                        }
                    )
            elif isinstance(event, TaskAttemptSucceeded | TaskAttemptFailed | TaskAttemptStopped):
                key = by_activation.get((event.activation_id, event.attempt))
                if key is not None:
                    running.pop(key, None)
        return running

    def _append(self, events: Sequence[RuntimeEvent], *, expected_next_seq: int | None = None) -> None:
        materialized = tuple(events)
        existing = self._ledger.read_all()
        if expected_next_seq is None:
            expected_next_seq = _next_sequence(existing)
        self._guard_transition()
        append_validated_batch(
            self._ledger,
            materialized,
            expected_next_seq=expected_next_seq,
        )
        self._guard_transition()

    def _append_prepared(self, events: Sequence[RuntimeEvent], *, expected_next_seq: int) -> None:
        materialized = tuple(events)
        try:
            self._guard_transition()
            append_validated_batch(
                self._ledger,
                materialized,
                expected_next_seq=expected_next_seq,
            )
            self._guard_transition()
        except LedgerPublicationIndeterminate as error:
            raise HeadPublicationIndeterminate(
                "prepared ledger publication outcome is indeterminate; candidate HEAD preserved"
            ) from error.__cause__

    def _guard_transition(self) -> None:
        if self._transition_guard is not None:
            self._transition_guard()

    def _now(self) -> float:
        value = self._clock.now()
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
            raise ValueError("clock returned a non-finite number")
        return float(value)

    def _attempt_id(self, task: PlannedTask, phase: str) -> str:
        identity = canonical_digest(
            {
                "attempt": task.attempt,
                "owner_id": self._owner_id,
                "phase": phase,
                "task_id": task.task_id,
            }
        )
        return f"{identity}.{phase}"


def _initial_batch_absent(
    ledger: Ledger,
    expected: tuple[EventEnvelope, ...],
    expected_next_seq: int,
) -> bool:
    try:
        persisted = ledger.read_all()
    except BaseException:
        return False
    offset = expected_next_seq - 1
    window = persisted[offset : offset + len(expected)]
    return window != expected


def _exception_message(prefix: str, error: BaseException) -> str:
    detail = str(error)
    suffix = f": {detail}" if detail else ""
    return f"{prefix} {type(error).__name__}{suffix}"


class _InvalidPreparedOutput(ValueError):
    """Raised when a successful outcome cannot be published as a prepared commit."""


def _prepared_event_range_digest(expected_next_seq: int, events: Sequence[RuntimeEvent]) -> str:
    envelopes = [
        EventEnvelope.from_event(expected_next_seq + offset, event).model_dump(mode="json")
        for offset, event in enumerate(events)
    ]
    return canonical_digest(cast(JSONValue, envelopes))


def _next_sequence(envelopes: Sequence[EventEnvelope]) -> int:
    return envelopes[-1].seq + 1 if envelopes else 1


def _validate_start_transition(task: PlannedTask, envelopes: Sequence[EventEnvelope]) -> None:
    try:
        projection = fold_events(tuple(envelopes))
    except ProjectionError as error:
        raise SchedulerStateError("persisted ledger cannot authorize task start") from error
    if projection.status != "running" or projection.invocation_id != task.invocation_id:
        raise SchedulerStateError(f"task invocation is not running: {task.invocation_id}")
    expected_task_id = canonical_digest({"activation_id": task.activation_id, "kind": "task"})
    if task.task_id != expected_task_id:
        raise SchedulerStateError(f"task id does not match activation: {task.activation_id}")
    activation = next(
        (item for item in projection.activations if item.activation_id == task.activation_id),
        None,
    )
    if activation is None:
        raise SchedulerStateError(f"task activation does not exist: {task.activation_id}")
    if (
        activation.status != "active"
        or activation.graph_instance_id != task.graph_instance_id
        or activation.node_id != task.node_id
    ):
        raise SchedulerStateError(f"task activation is not active or does not match: {task.activation_id}")
    graph = next(
        (item for item in projection.graph_instances if item.graph_instance_id == task.graph_instance_id),
        None,
    )
    if graph is None or graph.status != "running":
        raise SchedulerStateError(f"task graph is not running: {task.graph_instance_id}")
    if any(
        attempt.lease_task_id is not None and attempt.lease_task_id != task.task_id
        for attempt in activation.attempts
    ):
        raise SchedulerStateError(f"task identity does not match activation: {task.activation_id}")
    expected_attempt = len(activation.attempts) + 1
    if task.attempt != expected_attempt:
        raise SchedulerStateError(
            f"expected task attempt {expected_attempt}, found {task.attempt}: {task.activation_id}"
        )
    if activation.attempts and activation.attempts[-1].status != "failed":
        raise SchedulerStateError(
            f"task retry requires a failed prior attempt: {task.activation_id}/{task.attempt}"
        )
    prior_failure = activation.attempts[-1].failure if activation.attempts else None
    if task.prior_failure != prior_failure:
        raise SchedulerStateError(f"task prior failure does not match projection: {task.activation_id}")


def _validate_running_transition(task: PlannedTask, envelopes: Sequence[EventEnvelope]) -> None:
    try:
        projection = fold_events(tuple(envelopes))
    except ProjectionError as error:
        raise SchedulerStateError("persisted ledger cannot authorize task recovery") from error
    if projection.status != "running" or projection.invocation_id != task.invocation_id:
        raise SchedulerStateError(f"task invocation is not running: {task.invocation_id}")
    activation = next(
        (item for item in projection.activations if item.activation_id == task.activation_id),
        None,
    )
    if (
        activation is None
        or activation.status != "active"
        or activation.graph_instance_id != task.graph_instance_id
        or activation.node_id != task.node_id
        or not activation.attempts
        or activation.attempts[-1].status != "running"
        or activation.attempts[-1].attempt != task.attempt
    ):
        raise SchedulerStateError(f"task running attempt does not match: {task.activation_id}")
    prior_failure = activation.attempts[-2].failure if len(activation.attempts) > 1 else None
    if task.prior_failure != prior_failure:
        raise SchedulerStateError(f"task prior failure does not match projection: {task.activation_id}")


def _receipt_matches_activity(
    receipt: TaskHostTerminalReceipt,
    task: PlannedTask,
    activity: TaskActivitySnapshot,
) -> bool:
    return (
        receipt.invocation_id == task.invocation_id
        and receipt.task_id == task.task_id
        and receipt.activation_id == task.activation_id
        and receipt.attempt == task.attempt
        and receipt.activity_id == activity.activity_id
        and receipt.request_digest == activity.request_digest
        and receipt.workspace_identity_digest == activity.workspace_identity.attempt_identity_digest
        and receipt.dispatch_fingerprint_digest == activity.dispatch_fingerprint_digest
        and receipt.reference_digest == activity.reference_digest
        and receipt.outcome_digest
        == canonical_digest(cast(JSONValue, receipt.outcome.model_dump(mode="json")))
    )


def _lease_has_live_activity(projection: InvocationProjection, lease: Lease) -> bool:
    activation = next(
        (item for item in projection.activations if item.activation_id == lease.activation_id),
        None,
    )
    if activation is None or not activation.attempts or activation.attempts[-1].attempt != lease.attempt:
        return False
    attempt = activation.attempts[-1]
    return attempt.activity is not None and attempt.status in {"running", "effect_pending"}


def _replace_with_lease_failure(result: AttemptResult, message: str) -> AttemptResult:
    return result.model_copy(update={"outcome": TaskOutcome.failed("transient", message)})


def _same_lease_owner(persisted: Lease | None, claimed: Lease) -> bool:
    return persisted is not None and (
        persisted.task_id,
        persisted.activation_id,
        persisted.attempt,
        persisted.owner_id,
    ) == (
        claimed.task_id,
        claimed.activation_id,
        claimed.attempt,
        claimed.owner_id,
    )


__all__ = [
    "AttemptResult",
    "Clock",
    "FakeClock",
    "LedgerPublicationIndeterminate",
    "Lease",
    "LeaseUnavailableError",
    "RecoveryResult",
    "Scheduler",
    "SchedulerStateError",
    "SystemClock",
    "select_wave",
]
