"""确定性资源 wave 选择与真并行 Execute/Update（设计 §8.3 / §9 / §10）。

``select_wave`` 按 ``(topology_rank, declaration_index, task_id)`` 排序后贪心挑选
最大无冲突集合（受 ``max_parallel_tasks`` 约束）；冲突 task 留待下一 superstep。
``Scheduler.execute`` 用 ``ThreadPoolExecutor`` 真并行跑选中的 task：每个 attempt
先落 ``task_attempt_started``、建私有 workspace、登记 lease + heartbeat，再调
``NodeRunner``；成功时在同一 progression 事务中原子写入
``task_attempt_succeeded``（及可选的去重 ``budget_consumed``）。sibling 失败/
中断时不取消已提交 future，成功 write-set 保持 pending 不 materialize；全部
必需 sibling 成功后才 merge、``commit_tree_pointer``、``superstep_committed``
并幂等物化。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal

from assurance_agent.exceptions import AaError
from assurance_agent.workflow.core.events import read_events_strict
from assurance_agent.workflow.core.graph_events import (
    BudgetConsumedEvent,
    SuperstepCommittedEvent,
    TaskAttemptFailedEvent,
    TaskAttemptStartedEvent,
    TaskAttemptStoppedEvent,
    TaskAttemptSucceededEvent,
)
from assurance_agent.workflow.graph.resume_wire import build_graph_interrupted_event
from assurance_agent.workflow.core.graph_types import ErrorKind
from assurance_agent.workflow.core.progression import (
    ProgressionError,
    commit_tree_pointer,
    transaction,
)
from assurance_agent.workflow.graph.checkpoint import (
    CheckpointStore,
    dump_checkpoint_snapshot,
    fold_invocation_events,
    project_invocation,
    render_workflow_state_yaml,
)
from assurance_agent.workflow.graph.compiler import canonical_digest
from assurance_agent.workflow.graph.contracts import (
    ExecutionContractCatalog,
    ResourceClaims,
    ResourcePath,
    claims_conflict,
)
from assurance_agent.workflow.graph.leases import (
    Clock,
    LeaseRegistry,
    SystemClock,
    compute_next_retry_at,
    heartbeat_while,
    new_lease,
    next_attempt_decision,
)
from assurance_agent.workflow.graph.models import (
    ExecutableTask,
    GraphProjection,
    PlanResult,
    RuntimeContext,
    TaskResult,
    WaveResult,
)
from assurance_agent.workflow.graph.planner import apply_state_updates
from assurance_agent.workflow.graph.project_locks import (
    ProjectLockPathError,
    ProjectLockManager,
    ProjectPublication,
    ProjectPublicationError,
    ProjectPublicationStore,
    ProjectResourceConflict,
    ProjectResourceLockManager,
)
from assurance_agent.workflow.graph.schema_v2 import StateDef
from assurance_agent.workflow.graph.task_runner import NodeRunner
from assurance_agent.workflow.graph.workspace import (
    TaskWorkspace,
    TreeStore,
    WorkspaceBackend,
    WorkspaceError,
)
from assurance_agent.workflow.healing.allocation import commit_healing_allocation_ledger


class SchedulerError(AaError):
    """scheduler 基础设施失败：Update/lease/workspace 无法安全推进。"""


def select_wave(
    tasks: Sequence[ExecutableTask],
    *,
    max_parallel_tasks: int,
) -> tuple[ExecutableTask, ...]:
    """贪心选择当前最大无冲突 wave；冲突 task 留给后续 superstep。"""
    if max_parallel_tasks < 1:
        raise SchedulerError(f"max_parallel_tasks must be >= 1, got {max_parallel_tasks}")
    ordered = sorted(
        tasks,
        key=lambda task: (task.topology_rank, task.declaration_index, task.task_id),
    )
    selected: list[ExecutableTask] = []
    for task in ordered:
        if len(selected) >= max_parallel_tasks:
            break
        if any(_resources_conflict(task.resources, other.resources) for other in selected):
            continue
        selected.append(task)
    return tuple(selected)


def _resources_conflict(left: ResourceClaims, right: ResourceClaims) -> bool:
    """``global:exclusive`` 与任何 sibling 冲突（未知资源单独成 wave）；其余走 registry 规则。"""
    if "global:exclusive" in left.exclusive or "global:exclusive" in right.exclusive:
        return True
    return claims_conflict(left, right)


def _synchronized_paths_for_wave(
    wave: Sequence[ExecutableTask],
) -> tuple[ResourcePath, ...]:
    return tuple(
        sorted(
            {path for task in wave for path in task.resources.synchronized},
            key=lambda path: (path.root, path.pattern),
        )
    )


def _project_lock_tokens_for_wave(wave: Sequence[ExecutableTask]) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                token
                for task in wave
                if task.resources.synchronized
                for token in task.resources.exclusive
                if token.startswith("project:")
            }
        )
    )


@dataclass
class _PreparedAttempt:
    task: ExecutableTask
    attempt_id: str
    attempt_number: int
    workspace: TaskWorkspace | None
    bypass: bool = False
    bypass_write_set_id: str | None = None


@dataclass
class _SettledAttempt:
    task_id: str
    status: Literal["succeeded", "failed", "interrupted", "stopped"]
    write_set_id: str | None = None
    retry_at: str | None = None


class Scheduler:
    """确定性 wave 选择 + 结构化线程并行 + sibling settle + Update commit。"""

    def __init__(
        self,
        *,
        checkpoints: CheckpointStore,
        object_store: TreeStore,
        clock: Clock | None = None,
        workspace_backend: WorkspaceBackend | None = None,
        node_runner: NodeRunner | None = None,
        max_parallel_tasks: int = 4,
        contracts: ExecutionContractCatalog | None = None,
        state_defs: Mapping[str, StateDef] | None = None,
        lease_registry: LeaseRegistry | None = None,
        project_lock_manager: ProjectLockManager | None = None,
        project_lock_timeout_seconds: float = 5.0,
    ) -> None:
        if project_lock_timeout_seconds < 0:
            raise ValueError("project_lock_timeout_seconds must be non-negative")
        self._checkpoints = checkpoints
        self._objects = object_store
        self._clock = clock or SystemClock()
        self._workspaces = workspace_backend
        self._runner = node_runner
        self._max_parallel_tasks = max_parallel_tasks
        self._contracts = contracts
        self._state_defs = dict(state_defs or {})
        self._leases = lease_registry
        self._project_locks = project_lock_manager
        self._project_lock_timeout_seconds = project_lock_timeout_seconds

    def execute(
        self,
        plan: PlanResult,
        projection: GraphProjection,
        context: RuntimeContext,
    ) -> WaveResult:
        if self._workspaces is None or self._runner is None:
            raise SchedulerError("Scheduler requires workspace_backend and node_runner")
        wave = select_wave(plan.tasks, max_parallel_tasks=self._max_parallel_tasks)
        synchronized_paths = _synchronized_paths_for_wave(wave)
        if not synchronized_paths:
            return self._execute_wave(
                plan,
                projection,
                context,
                wave=wave,
                base_tree_id=projection.current_tree_id,
                synchronized_paths=(),
            )

        tokens = _project_lock_tokens_for_wave(wave)
        if not tokens:
            raise SchedulerError("synchronized wave has no project:* exclusive token")
        manager = self._project_locks or ProjectResourceLockManager(
            context.project_root,
            clock=self._clock,
        )
        try:
            with manager.acquire(tokens, timeout_seconds=self._project_lock_timeout_seconds):
                ProjectPublicationStore(context.project_root).assert_no_prepared(tokens)
                overlay_tree_id = self._objects.overlay_synchronized_paths(
                    projection.current_tree_id,
                    context.project_root,
                    synchronized_paths,
                )
                return self._execute_wave(
                    plan,
                    projection,
                    context,
                    wave=wave,
                    base_tree_id=overlay_tree_id,
                    synchronized_paths=synchronized_paths,
                )
        except ProjectResourceConflict as exc:
            return self._persist_project_lock_conflict(
                plan=plan,
                projection=projection,
                context=context,
                wave=wave,
                message=str(exc),
                blocked_token=exc.token,
            )
        except ProjectLockPathError as exc:
            raise SchedulerError(str(exc)) from None
        except ProjectPublicationError as exc:
            raise SchedulerError(str(exc)) from None

    def _execute_wave(
        self,
        plan: PlanResult,
        projection: GraphProjection,
        context: RuntimeContext,
        *,
        wave: tuple[ExecutableTask, ...],
        base_tree_id: str,
        synchronized_paths: tuple[ResourcePath, ...],
    ) -> WaveResult:
        leases = self._leases or LeaseRegistry(context.change_dir)

        succeeded: list[str] = []
        failed: list[str] = []
        interrupted: list[str] = []
        stopped: list[str] = []
        pending_writes: list[str] = []
        retry_ats: list[str] = []
        stop_submitting = False
        futures: dict[Future[_SettledAttempt], _PreparedAttempt] = {}

        with ThreadPoolExecutor(max_workers=self._max_parallel_tasks) as pool:
            for task in wave:
                if stop_submitting:
                    break
                done_now, _ = wait(tuple(futures), timeout=0, return_when=FIRST_COMPLETED)
                for finished in list(done_now):
                    if finished not in futures:
                        continue
                    settled = finished.result()
                    self._record_settled(
                        settled,
                        succeeded,
                        failed,
                        interrupted,
                        stopped,
                        pending_writes,
                        retry_ats,
                    )
                    if settled.status in ("failed", "interrupted", "stopped"):
                        stop_submitting = True
                    del futures[finished]
                if stop_submitting:
                    break

                if synchronized_paths:
                    prepared = self._begin_attempt(
                        task,
                        plan,
                        projection,
                        context,
                        leases,
                        base_tree_id=base_tree_id,
                    )
                else:
                    # Preserve the legacy call shape for fault-injection wrappers and
                    # ordinary Change-local execution.
                    prepared = self._begin_attempt(task, plan, projection, context, leases)
                if prepared is None:
                    decision = next_attempt_decision(task=task, projection=projection, now=self._clock.now())
                    if decision.kind == "wait" and decision.next_retry_at is not None:
                        retry_ats.append(decision.next_retry_at)
                    elif decision.kind in ("exhausted", "failed"):
                        failed.append(task.task_id)
                    continue
                if prepared.bypass:
                    succeeded.append(task.task_id)
                    if prepared.bypass_write_set_id is not None:
                        pending_writes.append(prepared.bypass_write_set_id)
                    continue

                future = pool.submit(
                    self._run_attempt,
                    prepared,
                    plan,
                    projection,
                    context,
                    leases,
                )
                futures[future] = prepared

            if futures:
                for finished in wait(tuple(futures), return_when="ALL_COMPLETED").done:
                    settled = finished.result()
                    self._record_settled(
                        settled,
                        succeeded,
                        failed,
                        interrupted,
                        stopped,
                        pending_writes,
                        retry_ats,
                    )

        succeeded = list(dict.fromkeys(succeeded))
        failed = list(dict.fromkeys(failed))
        interrupted = list(dict.fromkeys(interrupted))
        stopped = list(dict.fromkeys(stopped))
        pending_writes = list(dict.fromkeys(pending_writes))

        wave_ok = not failed and not interrupted and not retry_ats and not stopped
        required_ids = {task.task_id for task in wave}
        completed_ok = set(succeeded)
        if wave_ok and required_ids and required_ids <= completed_ok:
            try:
                pending_writes = self._commit_wave(
                    plan=plan,
                    projection=projection,
                    context=context,
                    succeeded_ids=succeeded,
                    base_tree_id=base_tree_id,
                    synchronized_paths=synchronized_paths,
                )
            except (WorkspaceError, ProgressionError, SchedulerError, ValueError):
                # Update 失败：保留 pending write-set，不推断成功物化。
                pass

        return WaveResult(
            superstep_id=plan.superstep_id,
            succeeded=tuple(succeeded),
            failed=tuple(failed),
            interrupted=tuple(interrupted),
            stopped=tuple(stopped),
            pending_write_set_ids=tuple(pending_writes),
            retry_at=min(retry_ats) if retry_ats else None,
        )

    def _persist_project_lock_conflict(
        self,
        *,
        plan: PlanResult,
        projection: GraphProjection,
        context: RuntimeContext,
        wave: tuple[ExecutableTask, ...],
        message: str,
        blocked_token: str | None,
    ) -> WaveResult:
        if blocked_token is None:
            task = next((candidate for candidate in wave if candidate.resources.synchronized), None)
        else:
            task = next(
                (
                    candidate
                    for candidate in wave
                    if candidate.resources.synchronized and blocked_token in candidate.resources.exclusive
                ),
                None,
            )
        if task is None:
            detail = f" for token {blocked_token}" if blocked_token is not None else ""
            raise SchedulerError(f"project lock conflict{detail} without an owning synchronized task")
        decision = next_attempt_decision(task=task, projection=projection, now=self._clock.now())
        if decision.kind != "start" or decision.attempt_number is None:
            retry_at = decision.next_retry_at if decision.kind == "wait" else None
            failed = (task.task_id,) if decision.kind in ("failed", "exhausted") else ()
            return WaveResult(
                superstep_id=plan.superstep_id,
                failed=failed,
                retry_at=retry_at,
            )

        attempt_number = decision.attempt_number
        attempt_id = f"{task.task_id}-a{attempt_number}"
        started_at = self._clock.now()
        lease_seconds = max(
            task.timeout_policy.heartbeat_seconds * 3.0,
            task.timeout_policy.heartbeat_seconds + 1.0,
        )
        lease_expires_at = (started_at + timedelta(seconds=lease_seconds)).isoformat()
        with transaction(context.change_dir) as txn:
            txn.append_strict(
                TaskAttemptStartedEvent(
                    type="task_attempt_started",
                    invocation_id=task.invocation_id,
                    checkpoint_ns=task.checkpoint_ns,
                    superstep_id=plan.superstep_id,
                    task_id=task.task_id,
                    attempt_id=attempt_id,
                    node_id=task.node_id,
                    input_sha256=task.input_sha256,
                    graph_digest=projection.graph_digest,
                    contract_digest=task.contract_digest,
                    attempt_number=attempt_number,
                    lease_expires_at=lease_expires_at,
                    started_at=started_at.isoformat(),
                )
            )
        settled = self._persist_failure(
            prepared=_PreparedAttempt(
                task=task,
                attempt_id=attempt_id,
                attempt_number=attempt_number,
                workspace=None,
            ),
            plan=plan,
            context=context,
            error_kind="conflict",
            message=message,
        )
        return WaveResult(
            superstep_id=plan.superstep_id,
            failed=(task.task_id,),
            retry_at=settled.retry_at,
        )

    @staticmethod
    def _record_settled(
        settled: _SettledAttempt,
        succeeded: list[str],
        failed: list[str],
        interrupted: list[str],
        stopped: list[str],
        pending_writes: list[str],
        retry_ats: list[str],
    ) -> None:
        if settled.status == "succeeded":
            succeeded.append(settled.task_id)
            if settled.write_set_id is not None:
                pending_writes.append(settled.write_set_id)
        elif settled.status == "failed":
            failed.append(settled.task_id)
            if settled.retry_at is not None:
                retry_ats.append(settled.retry_at)
        elif settled.status == "interrupted":
            interrupted.append(settled.task_id)
            if settled.write_set_id is not None:
                pending_writes.append(settled.write_set_id)
        else:
            stopped.append(settled.task_id)
            if settled.write_set_id is not None:
                pending_writes.append(settled.write_set_id)

    def _begin_attempt(
        self,
        task: ExecutableTask,
        plan: PlanResult,
        projection: GraphProjection,
        context: RuntimeContext,
        leases: LeaseRegistry,
        *,
        base_tree_id: str | None = None,
    ) -> _PreparedAttempt | None:
        assert self._workspaces is not None
        existing = projection.tasks.get(task.task_id)
        if existing is not None and existing.status == "succeeded":
            # resume：已投影成功的 sibling 绕过 handler，重新加入 pending wave。
            return _PreparedAttempt(
                task=task,
                attempt_id=existing.latest_attempt_id or f"{task.task_id}-a{max(existing.attempts_used, 1)}",
                attempt_number=max(existing.attempts_used, 1),
                workspace=None,
                bypass=True,
                bypass_write_set_id=existing.write_set_id,
            )
        if existing is not None and existing.status == "running":
            return None

        decision = next_attempt_decision(task=task, projection=projection, now=self._clock.now())
        if decision.kind != "start" or decision.attempt_number is None:
            return None

        attempt_number = decision.attempt_number
        attempt_id = f"{task.task_id}-a{attempt_number}"
        started_at = self._clock.now()
        lease_seconds = max(
            task.timeout_policy.heartbeat_seconds * 3.0,
            task.timeout_policy.heartbeat_seconds + 1.0,
        )
        lease_expires_at = (started_at + timedelta(seconds=lease_seconds)).isoformat()

        with transaction(context.change_dir) as txn:
            txn.append_strict(
                TaskAttemptStartedEvent(
                    type="task_attempt_started",
                    invocation_id=task.invocation_id,
                    checkpoint_ns=task.checkpoint_ns,
                    superstep_id=plan.superstep_id,
                    task_id=task.task_id,
                    attempt_id=attempt_id,
                    node_id=task.node_id,
                    input_sha256=task.input_sha256,
                    graph_digest=projection.graph_digest,
                    contract_digest=task.contract_digest,
                    attempt_number=attempt_number,
                    lease_expires_at=lease_expires_at,
                    started_at=started_at.isoformat(),
                )
            )

        workspace = self._workspaces.create(
            task_id=task.task_id,
            base_tree_id=base_tree_id or projection.current_tree_id,
            store=self._objects,
            side_effect_free=self._is_side_effect_free(task),
            claims=task.resources,
        )
        leases.upsert(
            new_lease(
                task_id=task.task_id,
                attempt_id=attempt_id,
                session_id=context.parent_session_id,
                started_at=started_at.isoformat(),
                lease_expires_at=lease_expires_at,
            )
        )
        return _PreparedAttempt(
            task=task,
            attempt_id=attempt_id,
            attempt_number=attempt_number,
            workspace=workspace,
        )

    def _run_attempt(
        self,
        prepared: _PreparedAttempt,
        plan: PlanResult,
        projection: GraphProjection,
        context: RuntimeContext,
        leases: LeaseRegistry,
    ) -> _SettledAttempt:
        assert self._runner is not None
        task = prepared.task
        workspace = prepared.workspace
        assert workspace is not None
        heartbeat_seconds = task.timeout_policy.heartbeat_seconds
        lease_extension = max(heartbeat_seconds * 3.0, heartbeat_seconds + 1.0)
        try:
            if task.evidence_bindings:
                from assurance_agent.workflow.graph.evidence import (
                    EvidenceResolutionError,
                    resolve_evidence_values,
                )
                from assurance_agent.workflow.graph.task_runner import task_failure

                try:
                    values = resolve_evidence_values(projection, task.evidence_bindings)
                except EvidenceResolutionError as exc:
                    return self._persist_result(
                        prepared=prepared,
                        plan=plan,
                        context=context,
                        result=task_failure("contract", f"evidence resolution failed: {exc}"),
                        workspace=workspace,
                    )
                task = task.model_copy(update={"resolved_evidence": values})
            with heartbeat_while(
                leases,
                task_id=task.task_id,
                attempt_id=prepared.attempt_id,
                clock=self._clock,
                heartbeat_seconds=heartbeat_seconds,
                lease_extension_seconds=lease_extension,
            ):
                result = self._runner.execute(task, workspace, context)
            return self._persist_result(
                prepared=prepared,
                plan=plan,
                context=context,
                result=result,
                workspace=workspace,
            )
        finally:
            leases.remove(task.task_id, prepared.attempt_id)
            workspace.cleanup()

    def _persist_result(
        self,
        *,
        prepared: _PreparedAttempt,
        plan: PlanResult,
        context: RuntimeContext,
        result: TaskResult,
        workspace: TaskWorkspace,
    ) -> _SettledAttempt:
        task = prepared.task
        if result.status == "failed":
            return self._persist_failure(
                prepared=prepared,
                plan=plan,
                context=context,
                error_kind=result.error_kind or "internal",
                message=result.error or "task failed",
            )

        if result.status == "stopped":
            reason = result.error or "stopped"
            if isinstance(result.value, dict):
                raw = result.value.get("reason")
                if isinstance(raw, str) and raw.strip():
                    reason = raw
            with transaction(context.change_dir) as txn:
                txn.append_strict(
                    TaskAttemptStoppedEvent(
                        type="task_attempt_stopped",
                        invocation_id=task.invocation_id,
                        checkpoint_ns=task.checkpoint_ns,
                        superstep_id=plan.superstep_id,
                        task_id=task.task_id,
                        attempt_id=prepared.attempt_id,
                        reason=reason,
                        value=result.value,
                    )
                )
            return _SettledAttempt(task_id=task.task_id, status="stopped", write_set_id=None)

        try:
            write_set_id = self._freeze_if_needed(task, result, workspace)
        except WorkspaceError as exc:
            return self._persist_failure(
                prepared=prepared,
                plan=plan,
                context=context,
                error_kind=_freeze_error_kind(exc),
                message=str(exc),
            )

        if result.status == "interrupted":
            if result.interrupt is not None:
                schema_version = self._checkpoints.project(task.invocation_id).event_schema_version
                with transaction(context.change_dir) as txn:
                    txn.append_strict(
                        TaskAttemptSucceededEvent(
                            type="task_attempt_succeeded",
                            invocation_id=task.invocation_id,
                            checkpoint_ns=task.checkpoint_ns,
                            superstep_id=plan.superstep_id,
                            task_id=task.task_id,
                            attempt_id=prepared.attempt_id,
                            write_set_id=write_set_id,
                            outputs_sha256=dict(result.outputs_sha256),
                            frozen_outputs=dict(result.frozen_outputs),
                            gate_report=result.gate_report,
                            state_updates=dict(result.state_updates),
                            value=result.value,
                        )
                    )
                    txn.append_strict(
                        build_graph_interrupted_event(
                            task=task,
                            interrupt=result.interrupt,
                            event_schema_version=schema_version,
                        )
                    )
            return _SettledAttempt(task_id=task.task_id, status="interrupted", write_set_id=write_set_id)

        self._persist_success(
            prepared=prepared,
            plan=plan,
            context=context,
            result=result,
            write_set_id=write_set_id,
        )
        return _SettledAttempt(task_id=task.task_id, status="succeeded", write_set_id=write_set_id)

    def _freeze_if_needed(
        self,
        task: ExecutableTask,
        result: TaskResult,
        workspace: TaskWorkspace,
    ) -> str | None:
        if result.write_set_id is not None:
            return result.write_set_id
        if self._is_side_effect_free(task):
            return None
        write_set = self._objects.freeze_write_set(
            workspace, claims=task.resources, outputs=_task_outputs(task)
        )
        return write_set.write_set_id

    def _persist_success(
        self,
        *,
        prepared: _PreparedAttempt,
        plan: PlanResult,
        context: RuntimeContext,
        result: TaskResult,
        write_set_id: str | None,
    ) -> None:
        task = prepared.task
        outputs = dict(result.outputs_sha256)
        if write_set_id is not None and not outputs:
            try:
                outputs = dict(self._objects.load_write_set(write_set_id).outputs_sha256)
            except WorkspaceError:
                outputs = {}
        frozen_wire = dict(result.frozen_outputs)
        with transaction(context.change_dir) as txn:
            txn.append_strict(
                TaskAttemptSucceededEvent(
                    type="task_attempt_succeeded",
                    invocation_id=task.invocation_id,
                    checkpoint_ns=task.checkpoint_ns,
                    superstep_id=plan.superstep_id,
                    task_id=task.task_id,
                    attempt_id=prepared.attempt_id,
                    write_set_id=write_set_id,
                    outputs_sha256=outputs,
                    frozen_outputs=frozen_wire,
                    gate_report=result.gate_report,
                    state_updates=dict(result.state_updates),
                    value=result.value,
                )
            )
            if task.budget is not None:
                live = fold_invocation_events(task.invocation_id, read_events_strict(context.change_dir))
                if not _budget_already_consumed(live, task):
                    txn.append_strict(
                        BudgetConsumedEvent(
                            type="budget_consumed",
                            invocation_id=task.invocation_id,
                            checkpoint_ns=task.checkpoint_ns,
                            graph_id=task.graph_id,
                            budget_id=task.budget.budget_id,
                            consumption_id=task.budget.consumption_id,
                            task_id=task.task_id,
                        )
                    )
        if task.target == "operation:allocate-healing-attempt" and isinstance(result.value, Mapping):
            allocation = result.value
            required = (
                "episode_id",
                "attempt_id",
                "attempt_number",
                "operation_id",
                "source_batch_id",
                "baseline_sha256",
                "entry_batch_id",
            )
            if all(
                isinstance(allocation.get(key), str) for key in required if key != "attempt_number"
            ) and isinstance(allocation.get("attempt_number"), int):
                commit_healing_allocation_ledger(
                    context.change_dir,
                    episode_id=str(allocation["episode_id"]),
                    attempt_id=str(allocation["attempt_id"]),
                    attempt_number=int(allocation["attempt_number"]),
                    operation_id=str(allocation["operation_id"]),
                    source_batch_id=str(allocation["source_batch_id"]),
                    baseline_sha256=str(allocation["baseline_sha256"]),
                    entry_batch_id=str(allocation["entry_batch_id"]),
                )

    def _persist_failure(
        self,
        *,
        prepared: _PreparedAttempt,
        plan: PlanResult,
        context: RuntimeContext,
        error_kind: ErrorKind,
        message: str,
    ) -> _SettledAttempt:
        task = prepared.task
        retryable = error_kind in task.retry_policy.retry_on and error_kind in task.retryable_errors
        next_retry: str | None = None
        if retryable and prepared.attempt_number < task.retry_policy.max_attempts:
            next_retry = compute_next_retry_at(
                task.retry_policy,
                task.task_id,
                prepared.attempt_number,
                self._clock.now(),
            )
        with transaction(context.change_dir) as txn:
            txn.append_strict(
                TaskAttemptFailedEvent(
                    type="task_attempt_failed",
                    invocation_id=task.invocation_id,
                    checkpoint_ns=task.checkpoint_ns,
                    superstep_id=plan.superstep_id,
                    task_id=task.task_id,
                    attempt_id=prepared.attempt_id,
                    error_kind=error_kind,
                    message=message,
                    next_retry_at=next_retry,
                )
            )
        return _SettledAttempt(task_id=task.task_id, status="failed", retry_at=next_retry)

    def _commit_wave(
        self,
        *,
        plan: PlanResult,
        projection: GraphProjection,
        context: RuntimeContext,
        succeeded_ids: list[str],
        base_tree_id: str | None = None,
        synchronized_paths: tuple[ResourcePath, ...] = (),
    ) -> list[str]:
        effective_base_tree_id = base_tree_id or projection.current_tree_id
        live = project_invocation(context.change_dir, projection.invocation_id)
        ordered_ids = sorted(succeeded_ids)
        write_sets = []
        state_pairs: list[tuple[str, Mapping[str, object]]] = []
        commit_eligible: list[str] = []
        for task_id in ordered_ids:
            task_proj = live.tasks.get(task_id)
            if task_proj is None or task_proj.status != "succeeded":
                continue
            if not task_proj.fan_out_child:
                commit_eligible.append(task_id)
            if task_proj.write_set_id is not None:
                write_sets.append(self._objects.load_write_set(task_proj.write_set_id))
            if task_proj.state_updates:
                state_pairs.append((task_id, task_proj.state_updates))

        recovered_paths = tuple(
            sorted(
                {
                    ResourcePath.parse(path)
                    for write_set in write_sets
                    for path in write_set.synchronized_paths
                },
                key=lambda path: (path.root, path.pattern),
            )
        )
        if recovered_paths and not synchronized_paths:
            recovered_tokens = tuple(
                sorted({token for write_set in write_sets for token in write_set.project_exclusive_tokens})
            )
            if not recovered_tokens:
                raise WorkspaceError("pending synchronized Update lacks project exclusive token metadata")
            manager = self._project_locks or ProjectResourceLockManager(
                context.project_root,
                clock=self._clock,
            )
            try:
                with manager.acquire(
                    recovered_tokens,
                    timeout_seconds=self._project_lock_timeout_seconds,
                ):
                    refreshed_base = self._objects.overlay_synchronized_paths(
                        projection.current_tree_id,
                        context.project_root,
                        recovered_paths,
                    )
                    if {write_set.base_tree_id for write_set in write_sets} != {refreshed_base}:
                        raise WorkspaceError(
                            "synchronized live resource changed before pending Update replay"
                        )
                    return self._commit_wave(
                        plan=plan,
                        projection=projection,
                        context=context,
                        succeeded_ids=succeeded_ids,
                        base_tree_id=refreshed_base,
                        synchronized_paths=recovered_paths,
                    )
            except ProjectResourceConflict as exc:
                raise SchedulerError(str(exc)) from None
            except ProjectLockPathError as exc:
                raise SchedulerError(str(exc)) from None

        committed_task_ids = sorted(commit_eligible)

        if write_sets:
            target_tree_id = self._objects.merge_write_sets(write_sets)
            committed_ids = [ws.write_set_id for ws in sorted(write_sets, key=lambda item: item.task_id)]
        else:
            target_tree_id = effective_base_tree_id
            committed_ids = []

        next_state = apply_state_updates(self._state_defs, live.state_values, state_pairs)
        checkpoint_id = canonical_digest(
            {
                "superstep_id": plan.superstep_id,
                "parent_checkpoint_id": plan.checkpoint_id,
                "write_set_ids": committed_ids,
                "target_tree_id": target_tree_id,
                "committed_task_ids": committed_task_ids,
            }
        )
        event = SuperstepCommittedEvent(
            type="superstep_committed",
            invocation_id=projection.invocation_id,
            checkpoint_ns=projection.checkpoint_ns,
            superstep_id=plan.superstep_id,
            checkpoint_id=checkpoint_id,
            parent_checkpoint_id=plan.checkpoint_id,
            write_set_ids=committed_ids,
            target_tree_id=target_tree_id,
            state_values=next_state,
            committed_task_ids=committed_task_ids,
        )
        publication: ProjectPublication | None = None
        publication_store: ProjectPublicationStore | None = None
        publication_status: Literal["prepared", "applied"] | None = None
        if synchronized_paths:
            publication_tokens = tuple(
                sorted({token for write_set in write_sets for token in write_set.project_exclusive_tokens})
            )
            if not publication_tokens:
                raise SchedulerError("synchronized Update lacks project publication tokens")
            publication = ProjectPublication(
                publication_id=checkpoint_id,
                invocation_id=projection.invocation_id,
                write_set_ids=tuple(committed_ids),
                tokens=publication_tokens,
            )
            publication_store = ProjectPublicationStore(context.project_root)
            try:
                publication_store.assert_no_prepared(
                    publication_tokens,
                    allowed_publication_id=checkpoint_id,
                )
                # Write-ahead reservation closes the commit/apply/ack crash windows:
                # later owners cannot advance these tokens until this publication is
                # durably acknowledged after exact targeted apply.
                publication_status = publication_store.prepare(publication)
            except (ProjectLockPathError, ProjectPublicationError) as exc:
                raise SchedulerError(str(exc)) from None
            except ProjectResourceConflict as exc:
                raise SchedulerError(str(exc)) from None
        tentative = live.model_copy(
            update={
                "latest_checkpoint_id": checkpoint_id,
                "current_tree_id": target_tree_id,
                "state_values": next_state,
            }
        )
        commit_tree_pointer(
            context.change_dir,
            event=event,
            checkpoint_rel=f".graph-runtime/checkpoints/{checkpoint_id}.json",
            checkpoint_bytes=dump_checkpoint_snapshot(tentative),
        )
        with transaction(context.change_dir) as txn:
            txn.set_workflow_state_projection(render_workflow_state_yaml(tentative))

        if write_sets:
            if synchronized_paths:
                assert publication is not None
                assert publication_store is not None
                if publication_status != "applied":
                    self._objects.apply_write_sets_to_synchronized_paths(
                        context.project_root,
                        write_sets,
                        synchronized_paths,
                    )
                    publication_store.acknowledge(publication)
            else:
                self._objects.apply_tree(
                    context.project_root,
                    target_tree_id,
                    base_tree_id=effective_base_tree_id,
                )
        return committed_ids

    def repair_committed_write_sets(
        self,
        *,
        context: RuntimeContext,
        invocation_id: str,
        publication_id: str,
        write_set_ids: Sequence[str],
    ) -> bool:
        """Replay one committed synchronized publication under its durable lock metadata.

        ``superstep_committed`` is intentionally durable before canonical publication.
        A recovery loop therefore cannot infer completion from the tree pointer and must
        replay the exact write-set entries. The operation is idempotent for fully and
        partially published Updates and never walks or repairs unrelated live paths.
        """
        write_sets = [self._objects.load_write_set(write_set_id) for write_set_id in write_set_ids]
        synchronized_paths = tuple(
            sorted(
                {
                    ResourcePath.parse(path)
                    for write_set in write_sets
                    for path in write_set.synchronized_paths
                },
                key=lambda path: (path.root, path.pattern),
            )
        )
        if not synchronized_paths:
            return False
        tokens = tuple(
            sorted({token for write_set in write_sets for token in write_set.project_exclusive_tokens})
        )
        if not tokens:
            raise SchedulerError("committed synchronized Update lacks project lock metadata")
        publication = ProjectPublication(
            publication_id=publication_id,
            invocation_id=invocation_id,
            write_set_ids=tuple(write_set_ids),
            tokens=tokens,
        )
        manager = self._project_locks or ProjectResourceLockManager(
            context.project_root,
            clock=self._clock,
        )
        try:
            with manager.acquire(tokens, timeout_seconds=self._project_lock_timeout_seconds):
                publication_store = ProjectPublicationStore(context.project_root)
                publication_store.assert_no_prepared(
                    tokens,
                    allowed_publication_id=publication_id,
                )
                publication_status = publication_store.prepare(publication)
                if publication_status != "applied":
                    self._objects.apply_write_sets_to_synchronized_paths(
                        context.project_root,
                        write_sets,
                        synchronized_paths,
                    )
                    publication_store.acknowledge(publication)
        except ProjectResourceConflict as exc:
            raise SchedulerError(str(exc)) from None
        except ProjectLockPathError as exc:
            raise SchedulerError(str(exc)) from None
        except ProjectPublicationError as exc:
            raise SchedulerError(str(exc)) from None
        return True

    def _is_side_effect_free(self, task: ExecutableTask) -> bool:
        if self._contracts is not None:
            contract = self._contracts.contracts.get(task.target)
            if contract is not None:
                return contract.side_effect_free
        claims = task.resources
        return not claims.writes and not claims.exclusive and not claims.authorization_writes


def _task_outputs(task: ExecutableTask) -> tuple[str, ...]:
    payload = task.input
    if isinstance(payload, Mapping):
        outputs = payload.get("outputs")
        if isinstance(outputs, list) and all(isinstance(item, str) for item in outputs):
            return tuple(outputs)
    return ()


def _budget_already_consumed(projection: GraphProjection, task: ExecutableTask) -> bool:
    if task.budget is None:
        return False
    existing = projection.tasks.get(task.task_id)
    return (
        existing is not None
        and existing.status == "succeeded"
        and projection.budgets.get(task.budget.budget_id, 0) > 0
    )


def _freeze_error_kind(exc: WorkspaceError) -> ErrorKind:
    message = str(exc).lower()
    if "output" in message:
        return "invalid_output"
    return "forbidden_write"


__all__ = [
    "Scheduler",
    "SchedulerError",
    "select_wave",
]
