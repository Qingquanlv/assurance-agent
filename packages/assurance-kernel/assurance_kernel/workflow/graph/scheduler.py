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

import hashlib
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel

from assurance_kernel.artifacts.canonical import canonical_json_bytes, sha256_bytes
from assurance_kernel.exceptions import AaError
from assurance_kernel.workflow.core.events import read_events_strict
from assurance_kernel.workflow.core.graph_events import (
    SuperstepCommittedEvent,
    TaskAttemptFailedEvent,
    TaskAttemptStartedEvent,
    TaskSchedulingDeferredEvent,
)
from assurance_kernel.workflow.core.progression import (
    ProgressionError,
    commit_tree_pointer,
    transaction,
)
from assurance_kernel.workflow.graph.checkpoint import (
    CheckpointStore,
    dump_checkpoint_snapshot,
    project_invocation,
    render_workflow_state_yaml,
)
from assurance_kernel.workflow.graph.compiler import canonical_digest
from assurance_kernel.workflow.graph.contracts import (
    ExecutionContractCatalog,
    ResourceClaims,
    ResourcePath,
    claims_conflict,
)
from assurance_kernel.workflow.graph.leases import (
    Clock,
    LeaseRegistry,
    SystemClock,
    compute_next_retry_at,
    next_attempt_decision,
)
from assurance_kernel.workflow.graph.models import (
    ArtifactReader,
    CompiledWorkflow,
    ExecutableTask,
    GraphProjection,
    PlanResult,
    RuntimeContext,
    TaskResult,
    WaveResult,
)
from assurance_kernel.workflow.graph.planner import apply_state_updates
from assurance_kernel.workflow.graph.project_locks import (
    ProjectLockPathError,
    ProjectLockManager,
    ProjectPublication,
    ProjectPublicationError,
    ProjectPublicationStore,
    ProjectResourceConflict,
    ProjectResourceLockManager,
)
from assurance_kernel.workflow.graph.schema_v2 import StateDef
from assurance_kernel.workflow.graph.task_runner import NodeRunner
from assurance_kernel.workflow.graph.durable_effects import (
    EffectRegistry,
    production_effect_registry,
)
from assurance_kernel.workflow.graph.artifact_commit import (
    committed_tree_targets as _committed_tree_targets_fn,
    last_committed_tree_edge as _last_committed_tree_edge_fn,
    repair_ordinary_materialization as _repair_ordinary_materialization_fn,
)
from assurance_kernel.workflow.graph.attempt_engine import (
    AttemptEngine,
    _PreparedAttempt,
    _SettledAttempt,
    _allow_graph_wrapper_child_resume,
)
from assurance_kernel.workflow.graph.workspace import (
    TaskWorkspace,
    TreeStore,
    WorkspaceBackend,
    WorkspaceError,
)

if TYPE_CHECKING:
    from assurance_kernel.workflow.graph.selected_wave import (
        PreparedWaveLease,
        SelectedInvocationWave,
    )


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
        effect_registry: EffectRegistry | None = None,
        crash_after_snapshot: Callable[[ExecutableTask, str], None] | None = None,
        crash_after_started: Callable[[ExecutableTask, str], None] | None = None,
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
        self._project_lock_scope_owner = object()
        self._active_project_lock_scopes: set[object] = set()
        self._prepared_wave_lease_owner = object()
        self._effect_registry = (
            effect_registry if effect_registry is not None else production_effect_registry()
        )
        # Test-only crash cuts between snapshot CAS and started append / after started.
        self._crash_after_snapshot = crash_after_snapshot
        self._crash_after_started = crash_after_started
        self._attempts = AttemptEngine(self)

    def select(self, plan: PlanResult) -> tuple[ExecutableTask, ...]:
        """Select the next executable wave from a planner result."""
        return select_wave(plan.tasks, max_parallel_tasks=self._max_parallel_tasks)

    def _finalize_selected_wave_reservation(
        self,
        wave: "SelectedInvocationWave",
        context: RuntimeContext,
        *,
        compiled: CompiledWorkflow,
        artifacts: ArtifactReader,
        child_projections: Mapping[str, GraphProjection] | None = None,
        inherited_lease: "PreparedWaveLease | None" = None,
    ) -> tuple[RuntimeContext, "PreparedWaveLease"]:
        """Repair, replan-verify, and attach a prepared lease under an already-held lock scope."""
        from assurance_kernel.workflow.core.progression import transaction
        from assurance_kernel.workflow.graph.selected_wave import (
            PreparedWaveLease,
            SelectedInvocationWave,
            build_prepared_wave_tree,
            flatten_prepared_invocations,
            iter_selected_waves,
        )

        assert isinstance(wave, SelectedInvocationWave)
        selected_wave = wave
        tokens = selected_wave.lock_tokens
        synchronized_paths = selected_wave.synchronized_paths
        if inherited_lease is not None:
            assert isinstance(inherited_lease, PreparedWaveLease)
            if inherited_lease.synchronized_paths:
                synchronized_paths = tuple(
                    sorted(
                        set(synchronized_paths) | set(inherited_lease.synchronized_paths),
                        key=lambda path: (path.root, path.pattern),
                    )
                )

        tree_ids = self._overlay_tree_ids_for_wave(selected_wave, context)
        if synchronized_paths and not (inherited_lease is not None and inherited_lease.capture_sealed):
            ordered_ids = tuple(sorted(tree_ids))
            overlays = self._objects.overlay_synchronized_paths_many(
                ordered_ids,
                context.project_root,
                synchronized_paths,
            )
            tree_overlays = dict(zip(ordered_ids, overlays, strict=True))
            capture_sealed = True
        elif inherited_lease is not None:
            tree_overlays = dict(inherited_lease.tree_overlays)
            capture_sealed = inherited_lease.capture_sealed
        else:
            tree_overlays = {tree_id: tree_id for tree_id in tree_ids}
            capture_sealed = False

        # Only this scheduler invocation owns ``context.project_root``. Each
        # selected descendant is repaired by its own ``GraphRuntime._drive``
        # after the graph wrapper enters that descendant's private task
        # workspace. Repairing descendants here applies their trees to the
        # outer root and leaks uncommitted outputs across the graph boundary.
        projection = self._checkpoints.project(selected_wave.invocation_id)
        self._repair_ordinary_materialization(projection, context, tree_overlays)

        verified = self._verify_selected_wave_tree(
            selected_wave,
            compiled=compiled,
            context=context,
            artifacts=artifacts,
            child_projections=child_projections,
        )
        for preview_wave in iter_selected_waves(verified):
            if preview_wave.plan.strict_events:
                with transaction(context.change_dir) as txn:
                    for event in preview_wave.plan.strict_events:
                        txn.append_strict(event)

        prepared_root = build_prepared_wave_tree(verified, tree_overlays)
        lease = PreparedWaveLease(
            lock_tokens=tokens,
            synchronized_paths=synchronized_paths,
            tree_overlays=tuple(sorted(tree_overlays.items())),
            invocations=flatten_prepared_invocations(prepared_root),
            capture_sealed=capture_sealed,
        )
        nonce = object()
        return context.with_prepared_wave_lease(
            self._prepared_wave_lease_owner,
            nonce,
            lease,
        ), lease

    def reserve_selected_wave(
        self,
        wave: "SelectedInvocationWave",
        context: RuntimeContext,
        *,
        compiled: CompiledWorkflow,
        artifacts: ArtifactReader,
        child_projections: Mapping[str, GraphProjection] | None = None,
        inherited_lease: "PreparedWaveLease | None" = None,
    ) -> tuple[RuntimeContext, "PreparedWaveLease"]:
        """Capture synchronized bytes once, repair, replan-verify, and attach a prepared lease."""
        from assurance_kernel.workflow.graph.selected_wave import SelectedInvocationWave

        assert isinstance(wave, SelectedInvocationWave)
        tokens = wave.lock_tokens
        synchronized_paths = wave.synchronized_paths
        if tokens:
            try:
                with self._project_lock_scope(context, tokens) as locked_context:
                    if synchronized_paths:
                        ProjectPublicationStore(locked_context.project_root).assert_no_prepared(tokens)
                    return self._finalize_selected_wave_reservation(
                        wave,
                        locked_context,
                        compiled=compiled,
                        artifacts=artifacts,
                        child_projections=child_projections,
                        inherited_lease=inherited_lease,
                    )
            except ProjectResourceConflict as exc:
                raise exc
            except ProjectLockPathError as exc:
                raise SchedulerError(str(exc)) from None
            except ProjectPublicationError as exc:
                raise SchedulerError(str(exc)) from None
        return self._finalize_selected_wave_reservation(
            wave,
            context,
            compiled=compiled,
            artifacts=artifacts,
            child_projections=child_projections,
            inherited_lease=inherited_lease,
        )

    def persist_project_lock_conflict(
        self,
        *,
        plan: PlanResult,
        projection: GraphProjection,
        context: RuntimeContext,
        wave: tuple[ExecutableTask, ...],
        exc: ProjectResourceConflict,
    ) -> WaveResult:
        """Map a project lock conflict into a retryable or failed wave result."""
        return self._persist_project_lock_conflict(
            plan=plan,
            projection=projection,
            context=context,
            wave=wave,
            message=str(exc),
            blocked_token=exc.token,
        )

    def execute_selected_wave(
        self,
        lease: "PreparedWaveLease",
        context: RuntimeContext,
        *,
        invocation_id: str,
        compiled: CompiledWorkflow | None = None,
        artifacts: ArtifactReader | None = None,
        child_projections: Mapping[str, GraphProjection] | None = None,
    ) -> WaveResult:
        """Execute one verified prepared invocation without recapturing synchronized bytes."""
        from assurance_kernel.workflow.graph.selected_wave import PreparedWaveLease

        assert isinstance(lease, PreparedWaveLease)
        tokens = lease.lock_tokens

        def _run(locked_context: RuntimeContext) -> WaveResult:
            # Verify only the invocation about to execute (and its descendants).
            # Replanning the lease root would see ancestor graph: wrappers already
            # marked running by the outer wave and spuriously return no selected wave.
            if compiled is not None and artifacts is not None:
                prepared = lease.for_invocation(invocation_id)
                if prepared is None:
                    raise SchedulerError(f"no prepared wave for invocation {invocation_id}")
                self._verify_selected_wave_tree(
                    prepared.preview,
                    compiled=compiled,
                    context=locked_context,
                    artifacts=artifacts,
                    child_projections=child_projections,
                )
            return self._execute_selected_wave_body(
                lease,
                locked_context,
                invocation_id=invocation_id,
            )

        if tokens:
            with self._project_lock_scope(context, tokens) as locked_context:
                return _run(locked_context)
        return _run(context)

    def _execute_selected_wave_body(
        self,
        lease: "PreparedWaveLease",
        context: RuntimeContext,
        *,
        invocation_id: str,
    ) -> WaveResult:
        from assurance_kernel.workflow.graph.selected_wave import PreparedWaveLease

        assert isinstance(lease, PreparedWaveLease)
        prepared = lease.for_invocation(invocation_id)
        if prepared is None:
            raise SchedulerError(f"no prepared wave for invocation {invocation_id}")
        preview = prepared.preview
        projection = self._checkpoints.project(invocation_id).model_copy(
            update={"current_tree_id": prepared.prepared_tree_id}
        )
        return self._execute_wave(
            preview.plan,
            projection,
            context,
            wave=preview.selected_tasks,
            base_tree_id=prepared.prepared_tree_id,
            synchronized_paths=lease.synchronized_paths,
        )

    def _overlay_tree_ids_for_wave(
        self,
        wave: "SelectedInvocationWave",
        context: RuntimeContext,
    ) -> set[str]:
        from assurance_kernel.workflow.graph.selected_wave import iter_selected_waves

        tree_ids: set[str] = set()
        for preview_wave in iter_selected_waves(wave):
            projection = self._checkpoints.project(preview_wave.invocation_id)
            tree_ids.add(projection.current_tree_id)
            prev, target, _, write_set_ids = self._last_committed_tree_edge(projection)
            if target is None:
                continue
            if write_set_ids:
                write_sets = [self._objects.load_write_set(write_set_id) for write_set_id in write_set_ids]
                if any(write_set.synchronized_paths for write_set in write_sets):
                    continue
            tree_ids.add(target)
            if prev is not None:
                tree_ids.add(prev)
        return tree_ids

    def _verify_selected_wave_tree(
        self,
        wave: "SelectedInvocationWave",
        *,
        compiled: CompiledWorkflow,
        context: RuntimeContext,
        artifacts: ArtifactReader,
        child_projections: Mapping[str, GraphProjection] | None,
    ) -> "SelectedInvocationWave":
        from assurance_kernel.workflow.graph.selected_wave import (
            assert_same_selected_wave,
            iter_selected_waves,
            preview_selected_wave,
        )

        lookup = dict(child_projections or {})
        for preview_wave in iter_selected_waves(wave):
            lookup[preview_wave.invocation_id] = self._checkpoints.project(preview_wave.invocation_id)
        verified_root: SelectedInvocationWave | None = None
        for preview_wave in iter_selected_waves(wave):
            projection = self._checkpoints.project(preview_wave.invocation_id)
            actual = preview_selected_wave(
                compiled,
                projection,
                context,
                artifacts,
                max_parallel_tasks=self._max_parallel_tasks,
                child_projections=lookup,
            )
            assert_same_selected_wave(preview_wave, actual)
            if preview_wave.invocation_id == wave.invocation_id:
                verified_root = actual
        if verified_root is None:
            raise SchedulerError("verified selected wave missing root invocation")
        return verified_root

    def _repair_ordinary_materialization(
        self,
        projection: GraphProjection,
        context: RuntimeContext,
        tree_overlays: Mapping[str, str],
    ) -> None:
        _repair_ordinary_materialization_fn(
            self._objects,
            projection,
            context,
            read_events_strict(self._checkpoints.change_dir),
            tree_overlays=tree_overlays,
        )

    def _committed_tree_targets(self, projection: GraphProjection) -> tuple[str, ...]:
        return _committed_tree_targets_fn(
            read_events_strict(self._checkpoints.change_dir),
            projection.invocation_id,
        )

    def _last_committed_tree_edge(
        self,
        projection: GraphProjection,
    ) -> tuple[str | None, str | None, str | None, tuple[str, ...]]:
        edge = _last_committed_tree_edge_fn(
            read_events_strict(self._checkpoints.change_dir),
            projection,
        )
        return edge.prev_tree_id, edge.target_tree_id, edge.publication_id, edge.write_set_ids

    def commit_pending_updates(
        self,
        *,
        superstep_id: str,
        checkpoint_id: str,
        projection: GraphProjection,
        context: RuntimeContext,
        succeeded_ids: list[str],
    ) -> list[str]:
        """Commit a planned superstep whose tasks already succeeded but were not materialised.

        Used by GraphRuntime pending-update recovery. Callers express intent only;
        the empty-plan commit shape stays inside the scheduler.
        """
        plan = PlanResult(
            superstep_id=superstep_id,
            checkpoint_id=checkpoint_id,
            tasks=(),
        )
        return self._commit_wave(
            plan=plan,
            projection=projection,
            context=context,
            succeeded_ids=succeeded_ids,
        )

    @contextmanager
    def _project_lock_scope(
        self,
        context: RuntimeContext,
        tokens: Sequence[str],
    ) -> Iterator[RuntimeContext]:
        """Acquire canonical project tokens once per nested runtime call stack.

        Graph nodes carry descendant footprints so the outermost scheduler can
        reserve every project token before materializing its task workspace.
        Descendant schedulers must therefore reuse that live reservation rather
        than opening a second, self-conflicting ``flock`` handle.
        """
        ordered = tuple(sorted(set(tokens)))
        inherited_scope = context.inherited_project_lock_scope(self._project_lock_scope_owner)
        if inherited_scope is not None and inherited_scope[0] in self._active_project_lock_scopes:
            _, inherited_tokens = inherited_scope
            inherited = frozenset(inherited_tokens)
            missing = sorted(set(ordered) - inherited)
            if missing:
                raise SchedulerError(
                    "nested synchronized wave requested tokens outside its ancestor footprint: "
                    + ", ".join(missing)
                )
            yield context
            return

        manager = self._project_locks or ProjectResourceLockManager(
            context.project_root,
            clock=self._clock,
        )
        with manager.acquire(ordered, timeout_seconds=self._project_lock_timeout_seconds):
            nonce = object()
            self._active_project_lock_scopes.add(nonce)
            try:
                yield context.with_project_lock_scope(
                    self._project_lock_scope_owner,
                    nonce,
                    ordered,
                )
            finally:
                self._active_project_lock_scopes.discard(nonce)

    def execute(
        self,
        plan: PlanResult,
        projection: GraphProjection,
        context: RuntimeContext,
        *,
        selected_wave: "SelectedInvocationWave | None" = None,
        compiled: CompiledWorkflow | None = None,
        artifacts: ArtifactReader | None = None,
        child_projections: Mapping[str, GraphProjection] | None = None,
        inherited_lease: "PreparedWaveLease | None" = None,
    ) -> WaveResult:
        if self._workspaces is None or self._runner is None:
            raise SchedulerError("Scheduler requires workspace_backend and node_runner")
        if selected_wave is not None and compiled is not None and artifacts is not None:
            from assurance_kernel.workflow.graph.selected_wave import (
                SelectedInvocationWave,
                SelectedWaveDriftError,
            )

            assert isinstance(selected_wave, SelectedInvocationWave)
            tokens = selected_wave.lock_tokens
            synchronized_paths = selected_wave.synchronized_paths
            try:
                if tokens:
                    with self._project_lock_scope(context, tokens) as locked_context:
                        if synchronized_paths:
                            ProjectPublicationStore(locked_context.project_root).assert_no_prepared(tokens)
                        locked_context, lease = self._finalize_selected_wave_reservation(
                            selected_wave,
                            locked_context,
                            compiled=compiled,
                            artifacts=artifacts,
                            child_projections=child_projections,
                            inherited_lease=inherited_lease,
                        )
                        return self._execute_selected_wave_body(
                            lease,
                            locked_context,
                            invocation_id=projection.invocation_id,
                        )
                locked_context, lease = self._finalize_selected_wave_reservation(
                    selected_wave,
                    context,
                    compiled=compiled,
                    artifacts=artifacts,
                    child_projections=child_projections,
                    inherited_lease=inherited_lease,
                )
                return self._execute_selected_wave_body(
                    lease,
                    locked_context,
                    invocation_id=projection.invocation_id,
                )
            except ProjectResourceConflict as exc:
                return self.persist_project_lock_conflict(
                    plan=plan,
                    projection=projection,
                    context=context,
                    wave=selected_wave.selected_tasks,
                    exc=exc,
                )
            except SelectedWaveDriftError:
                raise
            except ProjectLockPathError as exc:
                raise SchedulerError(str(exc)) from None
            except ProjectPublicationError as exc:
                raise SchedulerError(str(exc)) from None

        wave = self.select(plan)
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
        try:
            with self._project_lock_scope(context, tokens) as locked_context:
                ProjectPublicationStore(locked_context.project_root).assert_no_prepared(tokens)
                overlay_tree_id = self._objects.overlay_synchronized_paths(
                    projection.current_tree_id,
                    locked_context.project_root,
                    synchronized_paths,
                )
                return self._execute_wave(
                    plan,
                    projection,
                    locked_context,
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
                    decision = next_attempt_decision(
                        task=task,
                        projection=projection,
                        now=self._clock.now(),
                        allow_graph_wrapper_child_resume=_allow_graph_wrapper_child_resume(task, context),
                    )
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
        decision = next_attempt_decision(
            task=task,
            projection=projection,
            now=self._clock.now(),
            allow_graph_wrapper_child_resume=_allow_graph_wrapper_child_resume(task, context),
        )
        if decision.kind == "wait" and decision.next_retry_at is not None:
            return WaveResult(superstep_id=plan.superstep_id, retry_at=decision.next_retry_at)
        if decision.kind in ("failed", "exhausted"):
            return WaveResult(superstep_id=plan.superstep_id, failed=(task.task_id,))

        # Committed-but-unacked publications are not transient flock contention:
        # deferral would busy-spin (default backoff is 0s) until the foreign
        # change resumes. Fail closed so the later owner surfaces ``failed``.
        # Non-retryable conflict policies also fail closed via an attempt.
        conflict_retryable = "conflict" in task.retry_policy.retry_on and "conflict" in task.retryable_errors
        permanent_publication_block = "unacknowledged publication" in message
        if permanent_publication_block or not conflict_retryable:
            return self._fail_closed_project_resource_conflict(
                plan=plan,
                projection=projection,
                context=context,
                task=task,
                message=message,
                attempt_number=decision.attempt_number
                if decision.kind == "start" and decision.attempt_number is not None
                else 1,
                force_terminal=permanent_publication_block,
            )

        token = blocked_token or next(
            (item for item in task.resources.exclusive if item.startswith("project:")),
            "project:unknown",
        )
        prev = projection.tasks.get(task.task_id)
        ordinal = (prev.deferral_ordinal if prev is not None else 0) + 1
        retry_policy_digest = _retry_policy_digest(task)
        next_retry = compute_next_retry_at(
            task.retry_policy,
            task.task_id,
            ordinal,
            self._clock.now(),
        )
        deferral_id = _deferral_id(
            invocation_id=task.invocation_id,
            checkpoint_ns=task.checkpoint_ns,
            superstep_id=plan.superstep_id,
            task_id=task.task_id,
            token=token,
            ordinal=ordinal,
        )
        # Prepared/synchronized waves write plan.strict_events only after the
        # project lock is held. A conflict must still durable-ize node activation
        # so later due reselection has a generation binding; the superstep itself
        # remains uncommitted (D13).
        pending_plan_events = _unwritten_activation_events(context.change_dir, plan.strict_events)
        with transaction(context.change_dir) as txn:
            for event in pending_plan_events:
                txn.append_strict(event)
            txn.append_strict(
                TaskSchedulingDeferredEvent(
                    type="task_scheduling_deferred",
                    deferral_id=deferral_id,
                    invocation_id=task.invocation_id,
                    checkpoint_ns=task.checkpoint_ns,
                    superstep_id=plan.superstep_id,
                    task_id=task.task_id,
                    node_id=task.node_id,
                    token=token,
                    reason=message,
                    deferral_ordinal=ordinal,
                    retry_policy_digest=retry_policy_digest,
                    next_retry_at=next_retry,
                )
            )
        return WaveResult(superstep_id=plan.superstep_id, retry_at=next_retry)

    def _fail_closed_project_resource_conflict(
        self,
        *,
        plan: PlanResult,
        projection: GraphProjection,
        context: RuntimeContext,
        task: ExecutableTask,
        message: str,
        attempt_number: int,
        force_terminal: bool,
    ) -> WaveResult:
        """Record a conflict attempt failure; optional hard stop for unacked publications."""
        attempt_id = f"{task.task_id}-a{attempt_number}"
        started_at = self._clock.now()
        lease_seconds = max(
            task.timeout_policy.heartbeat_seconds * 3.0,
            task.timeout_policy.heartbeat_seconds + 1.0,
        )
        lease_expires_at = (started_at + timedelta(seconds=lease_seconds)).isoformat()
        pending_plan_events = _unwritten_activation_events(context.change_dir, plan.strict_events)
        with transaction(context.change_dir) as txn:
            for event in pending_plan_events:
                txn.append_strict(event)
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
        if force_terminal:
            with transaction(context.change_dir) as txn:
                txn.append_strict(
                    TaskAttemptFailedEvent(
                        type="task_attempt_failed",
                        invocation_id=task.invocation_id,
                        checkpoint_ns=task.checkpoint_ns,
                        superstep_id=plan.superstep_id,
                        task_id=task.task_id,
                        attempt_id=attempt_id,
                        error_kind="conflict",
                        message=message,
                        next_retry_at=None,
                    )
                )
            return WaveResult(superstep_id=plan.superstep_id, failed=(task.task_id,))
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
        return self._attempts._begin_attempt(
            task, plan, projection, context, leases, base_tree_id=base_tree_id
        )

    def _run_attempt(
        self,
        prepared: _PreparedAttempt,
        plan: PlanResult,
        projection: GraphProjection,
        context: RuntimeContext,
        leases: LeaseRegistry,
    ) -> _SettledAttempt:
        return self._attempts._run_attempt(prepared, plan, projection, context, leases)

    def _persist_result(
        self,
        *,
        prepared: _PreparedAttempt,
        plan: PlanResult,
        projection: GraphProjection,
        context: RuntimeContext,
        result: TaskResult,
        workspace: TaskWorkspace,
    ) -> _SettledAttempt:
        return self._attempts._persist_result(
            prepared=prepared,
            plan=plan,
            projection=projection,
            context=context,
            result=result,
            workspace=workspace,
        )

    def _persist_success(self, **kwargs: object) -> None:
        self._attempts._persist_success(**kwargs)  # type: ignore[arg-type]

    def _persist_failure(self, **kwargs: object) -> _SettledAttempt:
        return self._attempts._persist_failure(**kwargs)  # type: ignore[arg-type]

    def _freeze_if_needed(
        self,
        task: ExecutableTask,
        result: TaskResult,
        workspace: TaskWorkspace,
    ) -> str | None:
        return self._attempts._freeze_if_needed(task, result, workspace)

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
        # A recovery node can run in a later planner wave while successful
        # siblings from the failed wave still own uncommitted write-sets.  The
        # successful recovery closes that atomic boundary, so commit every
        # successful, uncommitted task together.  Already-committed write-sets
        # must stay out of this boundary: their older base_tree_id would
        # false-trigger "synchronized live resource changed" during pending
        # recovery even when live sync bytes are unchanged.
        # ``succeeded_ids`` remains part of the call signature for wave commit
        # sites; membership is always the live uncommitted success set.
        _ = succeeded_ids
        ordered_ids = sorted(
            task_id
            for task_id, task in live.tasks.items()
            if task.status == "succeeded" and not task.outputs_committed
        )
        if not ordered_ids:
            return []
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
            try:
                with self._project_lock_scope(context, recovered_tokens) as locked_context:
                    refreshed_base = self._objects.overlay_synchronized_paths(
                        projection.current_tree_id,
                        locked_context.project_root,
                        recovered_paths,
                    )
                    bases = {write_set.base_tree_id for write_set in write_sets}
                    if bases == {refreshed_base}:
                        commit_base = refreshed_base
                    elif len(bases) == 1:
                        # Pending sync tasks often leave their own outputs on
                        # disk before Update commit. Re-overlay then differs
                        # from the attempt-time base even with no external
                        # writer. Trust the shared write-set base; merge/apply
                        # before_sha256 checks still fail closed on conflicts.
                        commit_base = next(iter(bases))
                    else:
                        raise WorkspaceError(
                            "synchronized live resource changed before pending Update replay"
                        )
                    return self._commit_wave(
                        plan=plan,
                        projection=projection,
                        context=locked_context,
                        succeeded_ids=succeeded_ids,
                        base_tree_id=commit_base,
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
        try:
            with self._project_lock_scope(context, tokens) as locked_context:
                publication_store = ProjectPublicationStore(locked_context.project_root)
                publication_store.assert_no_prepared(
                    tokens,
                    allowed_publication_id=publication_id,
                )
                publication_status = publication_store.prepare(publication)
                if publication_status == "applied":
                    return False
                self._objects.apply_write_sets_to_synchronized_paths(
                    locked_context.project_root,
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

    def commit_pending_write_sets(
        self,
        *,
        plan: PlanResult,
        projection: GraphProjection,
        context: RuntimeContext,
        succeeded_ids: list[str],
    ) -> bool:
        """Commit succeeded tasks from an uncommitted superstep; return True when durable progress."""
        before = {
            raw.get("superstep_id")
            for raw in read_events_strict(context.change_dir)
            if raw.get("source") == "graph"
            and raw.get("invocation_id") == projection.invocation_id
            and raw.get("type") == "superstep_committed"
            and isinstance(raw.get("superstep_id"), str)
        }
        self._commit_wave(
            plan=plan,
            projection=projection,
            context=context,
            succeeded_ids=succeeded_ids,
        )
        after = {
            raw.get("superstep_id")
            for raw in read_events_strict(context.change_dir)
            if raw.get("source") == "graph"
            and raw.get("invocation_id") == projection.invocation_id
            and raw.get("type") == "superstep_committed"
            and isinstance(raw.get("superstep_id"), str)
        }
        return plan.superstep_id not in before and plan.superstep_id in after


def _retry_policy_digest(task: ExecutableTask) -> str:
    payload = {
        "backoff": task.retry_policy.backoff.model_dump(mode="json"),
        "max_attempts": task.retry_policy.max_attempts,
        "retry_on": list(task.retry_policy.retry_on),
        "task_id": task.task_id,
    }
    return sha256_bytes(canonical_json_bytes(payload))


def _deferral_id(
    *,
    invocation_id: str,
    checkpoint_ns: str,
    superstep_id: str,
    task_id: str,
    token: str,
    ordinal: int,
) -> str:
    material = "|".join((invocation_id, checkpoint_ns, superstep_id, task_id, token, str(ordinal)))
    return "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def _unwritten_activation_events(change_dir: Path, events: Sequence[BaseModel]) -> tuple[BaseModel, ...]:
    """Return node_activated events from a plan that are not yet durable."""
    if not events:
        return ()
    existing = read_events_strict(change_dir)
    activated = {
        (event.get("invocation_id"), event.get("node_id"), event.get("generation_ordinal"))
        for event in existing
        if event.get("type") == "node_activated"
    }
    pending: list[BaseModel] = []
    for event in events:
        if getattr(event, "type", None) != "node_activated":
            continue
        key = (
            getattr(event, "invocation_id", None),
            getattr(event, "node_id", None),
            getattr(event, "generation_ordinal", None),
        )
        if key in activated:
            continue
        pending.append(event)
    return tuple(pending)


__all__ = [
    "Scheduler",
    "SchedulerError",
    "select_wave",
]
