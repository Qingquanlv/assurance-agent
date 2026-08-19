from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from graph_engine.canonical import canonical_digest
from graph_engine.plugin_api import (
    CandidateWriteSet,
    CapabilityRegistry,
    FailureKind,
    ResourceClaims,
    TaskContext,
    TaskOutcome,
    TaskRequest,
    ValidationContext,
)
from graph_engine.runtime.events import (
    EventEnvelope,
    HeadAdvanced,
    RuntimeEvent,
    TaskAttemptFailed,
    TaskAttemptStarted,
    TaskAttemptStopped,
    TaskAttemptSucceeded,
    TaskLeaseAcquired,
    TaskLeaseHeartbeat,
)
from graph_engine.runtime.frozen_json import thaw_json
from graph_engine.runtime.ledger import Ledger
from graph_engine.runtime.models import CommitResult, PlannedTask
from graph_engine.runtime.workspace import AttemptWorkspace, SnapshotStore


class Clock(Protocol):
    def now(self) -> float: ...


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
        registry: CapabilityRegistry,
        store: SnapshotStore,
        ledger: Ledger,
        *,
        owner_id: str,
        clock: Clock | None = None,
        lease_seconds: float = 30.0,
        max_parallel: int = 1,
    ) -> None:
        if not owner_id:
            raise ValueError("owner_id must not be empty")
        if not math.isfinite(lease_seconds) or lease_seconds <= 0:
            raise ValueError("lease_seconds must be finite and positive")
        if not isinstance(max_parallel, int) or isinstance(max_parallel, bool) or max_parallel < 1:
            raise ValueError("max_parallel must be a positive integer")
        self._registry = registry
        self._store = store
        self._ledger = ledger
        self._owner_id = owner_id
        self._clock = clock or SystemClock()
        self._lease_seconds = lease_seconds
        self._max_parallel = max_parallel

    async def run_wave(self, tasks: Sequence[PlannedTask]) -> tuple[AttemptResult, ...]:
        selected = select_wave(tasks, self._max_parallel)
        if not selected:
            return ()

        work: list[tuple[PlannedTask, AttemptWorkspace, _LeaseState]] = []
        try:
            for task in selected:
                attempt_workspace = self._store.create_attempt(self._attempt_id(task, "run"))
                try:
                    lease = self._start(task)
                except BaseException:
                    attempt_workspace.discard()
                    raise
                work.append((task, attempt_workspace, _LeaseState(self, lease)))
        except BaseException:
            for _task, workspace, _lease_state in work:
                workspace.discard()
            raise

        gathered = await asyncio.gather(
            *(self._execute(task, workspace, lease_state) for task, workspace, lease_state in work)
        )
        finalized: list[AttemptResult] = []
        for result in gathered:
            finalized.append(self._finalize(result))
        return tuple(finalized)

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
        if expired:
            failure = TaskOutcome.failed("transient", "persisted task lease expired").failure
            assert failure is not None
            self._append(
                tuple(
                    TaskAttemptFailed(
                        activation_id=lease.activation_id,
                        attempt=lease.attempt,
                        failure=failure,
                    )
                    for lease in expired
                ),
                expected_next_seq=_next_sequence(envelopes),
            )
        return tuple(lease.task_id for lease in expired)

    def _start(self, task: PlannedTask) -> Lease:
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
        self._append(
            (
                TaskAttemptStarted(
                    activation_id=task.activation_id,
                    attempt=task.attempt,
                    lease_expires_at=format(lease.expires_at, ".17g"),
                ),
                TaskLeaseAcquired(**lease.model_dump()),
            )
        )
        return lease

    async def _execute(
        self,
        task: PlannedTask,
        workspace: AttemptWorkspace,
        lease_state: _LeaseState,
    ) -> AttemptResult:
        try:
            handler = self._registry.task_handlers.get(task.capability_id)
            if handler is None:
                return AttemptResult(
                    task=task,
                    outcome=TaskOutcome.failed("internal", f"missing task handler: {task.capability_id}"),
                    lease=lease_state.current,
                )
            request = TaskRequest(
                invocation_id=task.invocation_id,
                task_id=task.task_id,
                graph_instance_id=task.graph_instance_id,
                node_id=task.node_id,
                capability_id=task.capability_id,
                attempt=task.attempt,
                input=thaw_json(task.input),
                prior_failure=task.prior_failure,
            )

            context = TaskContext(
                workspace_root=workspace.root,
                heartbeat=lease_state.heartbeat,
            )
            try:
                async with asyncio.timeout(task.timeout_seconds):
                    outcome = await handler(request, context)
            except TimeoutError:
                outcome = TaskOutcome.failed("timeout", "task handler exceeded its run timeout")
            except asyncio.CancelledError as error:
                outcome = TaskOutcome.failed("internal", _exception_message("task handler raised", error))
            except Exception as error:
                outcome = TaskOutcome.failed("internal", _exception_message("task handler raised", error))
            if not isinstance(outcome, TaskOutcome):
                outcome = TaskOutcome.failed(
                    "internal",
                    f"task handler returned {type(outcome).__name__}, expected TaskOutcome",
                )
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
            workspace.discard()

    def _finalize(self, result: AttemptResult) -> AttemptResult:
        task = result.task
        guard = self._lease_guard(result.lease)
        if guard.running != result.lease:
            return _replace_with_lease_failure(result, "task lease is no longer the persisted running lease")
        if result.lease.expires_at < self._now():
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
        previous_tree_id: str | None = None
        try:
            previous_tree_id = self._store.head_tree_id()
            if candidate.baseline_tree_id != previous_tree_id:
                candidate = self._store.rebase_candidate(candidate, self._attempt_id(task, "rebase"))
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
            commit = self._store.commit_candidate(
                candidate,
                task.resources,
                validators,
                context,
            )
        except Exception as error:
            try:
                if previous_tree_id is not None and self._store.head_tree_id() == candidate.candidate_tree_id:
                    self._store._write_head(previous_tree_id)
            except Exception:
                raise error
            return self._record_commit_failure(
                result,
                _exception_message("candidate commit failed", error),
                expected_next_seq=guard.expected_next_seq,
            )
        if not commit.committed:
            reasons = "; ".join(
                f"{receipt.validator_id}: {receipt.reason}"
                for receipt in commit.receipts
                if not receipt.accepted
            )
            return self._record_commit_failure(
                result,
                f"commit validation rejected: {reasons}",
                kind="invalid_output",
                commit=commit,
                expected_next_seq=guard.expected_next_seq,
            )

        assert previous_tree_id is not None
        head_tree_id = self._store.head_tree_id()
        try:
            self._append(
                (
                    TaskAttemptSucceeded(
                        activation_id=task.activation_id,
                        attempt=task.attempt,
                        output=result.outcome.output,
                    ),
                    HeadAdvanced(
                        task_id=task.task_id,
                        activation_id=task.activation_id,
                        attempt=task.attempt,
                        previous_tree_id=previous_tree_id,
                        tree_id=head_tree_id,
                    ),
                ),
                expected_next_seq=guard.expected_next_seq,
            )
        except BaseException:
            self._store._write_head(previous_tree_id)
            raise
        return result.model_copy(
            update={"candidate": candidate, "commit": commit, "head_tree_id": head_tree_id}
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

    def _lease_guard(self, lease: Lease) -> _LeaseGuard:
        envelopes = self._ledger.read_all()
        running = self._persisted_running_leases(envelopes).get((lease.task_id, lease.attempt))
        return _LeaseGuard(expected_next_seq=_next_sequence(envelopes), running=running)

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
        if expected_next_seq is None:
            existing = self._ledger.read_all()
            expected_next_seq = _next_sequence(existing)
        self._ledger.append_batch(events, expected_next_seq=expected_next_seq)

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


def _exception_message(prefix: str, error: BaseException) -> str:
    detail = str(error)
    suffix = f": {detail}" if detail else ""
    return f"{prefix} {type(error).__name__}{suffix}"


def _next_sequence(envelopes: Sequence[EventEnvelope]) -> int:
    return envelopes[-1].seq + 1 if envelopes else 1


def _replace_with_lease_failure(result: AttemptResult, message: str) -> AttemptResult:
    return result.model_copy(update={"outcome": TaskOutcome.failed("transient", message)})


__all__ = [
    "AttemptResult",
    "Clock",
    "FakeClock",
    "Lease",
    "Scheduler",
    "SystemClock",
    "select_wave",
]
