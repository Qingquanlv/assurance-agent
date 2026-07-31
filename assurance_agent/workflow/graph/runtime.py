"""Ledger-driven GraphRuntime：Plan → Execute → Update 直到稳定返回边界。"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import uuid4

from assurance_agent.artifacts.policy import PolicyError
from assurance_agent.exceptions import AaError
from assurance_agent.workflow.core.events import LedgerIntegrityError, read_events_strict
from assurance_agent.workflow.core.exit_codes import (
    EXIT_COMPLETED,
    EXIT_ERROR,
    EXIT_HUMAN_REVIEW,
    EXIT_STOPPED,
)
from assurance_agent.workflow.core.graph_events import (
    BudgetConsumedEvent,
    CheckpointImportedEvent,
    GraphInvocationStartedEvent,
    GraphResumedEvent,
    GraphTerminalEvent,
    ResumeAnchor,
    SuperstepCommittedEvent,
    SuperstepPlannedEvent,
    TaskImportedEvent,
)
from assurance_agent.workflow.core.progression import ProgressionError, transaction
from assurance_agent.workflow.execution.tree_hash import sha256_file
from assurance_agent.workflow.graph.checkpoint import (
    CheckpointImportError,
    CheckpointStore,
    render_workflow_state_yaml,
    validate_import,
)
from assurance_agent.workflow.graph.status import (
    graph_status_from_projection,
    pending_write_sets as _pending_write_sets_fn,
)
from assurance_agent.workflow.graph.definition_pinning import (
    InvocationDefinitionBinding,
    bind_root_definitions,
    inherit_child_definitions,
    stage_pinned_definitions,
    verify_pinned_definitions,
)
from assurance_agent.workflow.graph.compiler import canonical_digest, resolve_params
from assurance_agent.workflow.graph.selected_wave import (
    SelectedWaveDriftError,
    derive_child_invocation_id,
    preview_selected_wave,
)
from assurance_agent.workflow.graph.ingest_catalog import validate_catalog_runtime
from assurance_agent.workflow.graph.contracts import ExecutionContractCatalog
from assurance_agent.workflow.graph.leases import (
    Clock,
    LeaseRegistry,
    SystemLivenessProbe,
    recover_running_tasks,
)
from assurance_agent.workflow.graph.models import (
    CompiledWorkflow,
    ExecutableTask,
    GraphProjection,
    GraphStatus,
    ImportManifest,
    ImportResult,
    InterruptProjection,
    PlanResult,
    ResumeCommand,
    RunResult,
    RuntimeContext,
    TaskResult,
)
from assurance_agent.workflow.graph.planner import PlanError, plan_superstep
from assurance_agent.workflow.graph.project_locks import ProjectPublicationStore
from assurance_agent.workflow.graph.scheduler import Scheduler, SchedulerError
from assurance_agent.workflow.graph.task_runner import NodeRunner
from assurance_agent.workflow.graph.workspace import (
    TaskWorkspace,
    TreeStore,
    WorkspaceBackend,
    WorkspaceError,
)
from assurance_agent.workflow.orchestration.dsl import Scope, is_satisfied


def _ingest_catalog_digest(compiled: CompiledWorkflow) -> str:
    digest = compiled.ingest_catalog_digest
    if digest:
        return digest
    return validate_catalog_runtime().digest


def _build_invocation_started(
    *,
    invocation_id: str,
    entrypoint: str,
    graph_id: str,
    compiled: CompiledWorkflow,
    params: dict[str, object],
    root_tree_id: str,
    max_parallel_tasks: int,
    checkpoint_ns: str,
    structural_path: str,
    binding: InvocationDefinitionBinding,
    parent_invocation_id: str | None = None,
    parent_task_id: str | None = None,
) -> GraphInvocationStartedEvent:
    catalog_digest = _ingest_catalog_digest(compiled)
    if not catalog_digest:
        raise GraphRuntimeError("ingest catalog digest is empty; cannot start graph invocation")
    return GraphInvocationStartedEvent(
        type="graph_invocation_started",
        invocation_id=invocation_id,
        entrypoint=entrypoint,
        graph_id=graph_id,
        graph_digest=compiled.digest,
        event_schema_version=binding.event_schema_version,
        ir_digest=compiled.digest,
        ingest_catalog_digest=catalog_digest,
        contract_digests=dict(compiled.contract_digests),
        policy_digest=binding.policy_digest,
        policy_origin=binding.policy_origin,
        gate_semantics_digest=binding.gate_semantics_digest,
        assurance_profile_digest=binding.assurance_profile_digest,
        params=params,
        params_sha256=canonical_digest(params),
        root_tree_id=root_tree_id,
        max_parallel_tasks=max_parallel_tasks,
        checkpoint_ns=checkpoint_ns,
        parent_invocation_id=parent_invocation_id,
        parent_task_id=parent_task_id,
        structural_path=structural_path,
    )


class GraphRuntimeError(AaError):
    """GraphRuntime 基础设施或契约失败；CLI 映射为 exit 40。"""


class GraphDefinitionChanged(GraphRuntimeError):
    """pinned graph/contract digest 与当前定义漂移；拒绝普通 resume。"""


class GraphIntegrityError(GraphRuntimeError):
    """checkpoint/ledger 损坏或因果完整性失败。"""


class GraphRuntime:
    """对外同步的 Plan → Execute → Update 驱动器；并发仅在 Scheduler 内部。"""

    def __init__(
        self,
        *,
        checkpoint_store: CheckpointStore,
        object_store: TreeStore,
        workspace_backend: WorkspaceBackend,
        contracts: ExecutionContractCatalog,
        node_runner: NodeRunner,
        scheduler: Scheduler,
        schema_resolver: Callable[[str], CompiledWorkflow],
        clock: Clock,
    ) -> None:
        self._checkpoints = checkpoint_store
        self._objects = object_store
        self._workspaces = workspace_backend
        self._contracts = contracts
        self._node_runner = node_runner
        self._scheduler = scheduler
        self._schema_resolver = schema_resolver
        self._clock = clock

    def run(
        self,
        schema: CompiledWorkflow,
        entrypoint: str,
        context: RuntimeContext,
    ) -> RunResult:
        invocation_id = self.start_invocation(schema, entrypoint, context)
        return self.drive_started(invocation_id)

    def start_invocation(
        self,
        compiled: CompiledWorkflow,
        entrypoint: str,
        context: RuntimeContext,
    ) -> str:
        """Validate params and atomically commit graph_invocation_started (+ pin defs)."""
        return self._start_invocation(compiled, entrypoint, context)

    def drive_started(self, invocation_id: str) -> RunResult:
        """Drive an already-started root invocation to completion or interrupt."""
        try:
            projection = self._checkpoints.project(invocation_id)
        except LedgerIntegrityError as exc:
            raise GraphIntegrityError(str(exc)) from exc
        context = self._context_for(projection)
        return self._drive(invocation_id, context)

    def resume(
        self,
        invocation_id: str,
        command: ResumeCommand | None = None,
    ) -> RunResult:
        return self._recover_and_drive(invocation_id, command)

    def status(self, invocation_id: str) -> GraphStatus:
        projection = self._checkpoints.project(invocation_id)
        return graph_status_from_projection(
            projection,
            pending_write_sets=self._pending_write_sets(invocation_id),
        )

    def import_checkpoint(
        self,
        schema: CompiledWorkflow,
        manifest: ImportManifest,
        context: RuntimeContext,
    ) -> ImportResult:
        """校验并原子导入显式 manifest；不伪造物理 attempt，随后 resume 继续。"""
        validated = validate_import(schema, manifest, context)
        latest = self.latest_root_invocation()
        if latest is not None:
            try:
                existing = self._checkpoints.project(latest)
            except LedgerIntegrityError as exc:
                raise GraphIntegrityError(str(exc)) from exc
            if existing.terminal is None:
                raise GraphRuntimeError(
                    f"change already has active invocation {latest}; resume instead of import"
                )

        if manifest.entrypoint not in schema.entrypoints:
            raise CheckpointImportError(f"unknown entrypoint '{manifest.entrypoint}'")
        entry = schema.entrypoints[manifest.entrypoint]
        try:
            params = resolve_params(schema.schema, {**entry.param_overrides, **context.params})
        except Exception as exc:
            raise GraphRuntimeError(f"invalid params: {exc}") from exc
        if manifest.entrypoint == "retro":
            try:
                params = ensure_retro_params(params)
            except Exception as exc:
                raise GraphRuntimeError(f"invalid retro params: {exc}") from exc
        if entry.allow_expr is not None and not is_satisfied(entry.allow_expr, Scope({"params": params})):
            raise GraphRuntimeError(f"entrypoint '{manifest.entrypoint}' allow expression rejected params")

        root_tree_id = self._objects.capture(context.project_root, repo_root=context.repo_root)
        invocation_id = str(uuid4())
        checkpoint_ns = invocation_id
        bound = context.model_copy(update={"params": params})
        try:
            binding = bind_root_definitions(store=self._objects, root_tree_id=root_tree_id)
        except PolicyError:
            raise
        started = _build_invocation_started(
            invocation_id=invocation_id,
            entrypoint=manifest.entrypoint,
            graph_id=entry.graph_id,
            compiled=schema,
            params=params,
            root_tree_id=root_tree_id,
            max_parallel_tasks=schema.schema.policies.scheduler.max_parallel_tasks,
            checkpoint_ns=checkpoint_ns,
            structural_path=entry.graph_id,
            binding=binding,
        )

        imported_task_ids: list[str] = []
        try:
            with transaction(context.change_dir) as txn:
                txn.append_strict(started)
                self._stage_pinned_definitions(txn, schema, binding)
                txn.write_runtime_file(
                    f".graph-runtime/invocations/{invocation_id}.json",
                    json.dumps(
                        {
                            "project_root": str(context.project_root),
                            "repo_root": str(context.repo_root),
                            "change_id": context.change_id,
                            "parent_session_id": context.parent_session_id,
                        },
                        sort_keys=True,
                    ).encode("utf-8"),
                )
                for item in validated.resolved:
                    txn.append_strict(
                        TaskImportedEvent(
                            type="task_imported",
                            invocation_id=invocation_id,
                            checkpoint_ns=checkpoint_ns,
                            graph_id=item.task.graph,
                            node_id=item.task.node,
                            structural_path=item.structural_path,
                            task_key=item.task.task_key,
                            outputs_sha256=dict(item.task.outputs),
                            gate_report=item.gate_report,
                        )
                    )
                    imported_task_ids.append(item.task_id)
                for budget in validated.budgets:
                    txn.append_strict(
                        BudgetConsumedEvent(
                            type="budget_consumed",
                            invocation_id=invocation_id,
                            checkpoint_ns=checkpoint_ns,
                            graph_id=entry.graph_id,
                            budget_id=budget.budget_id,
                            consumption_id=budget.consumption_id,
                            task_id=budget.task_path,
                        )
                    )
                txn.append_strict(
                    CheckpointImportedEvent(
                        type="checkpoint_imported",
                        invocation_id=invocation_id,
                        checkpoint_ns=checkpoint_ns,
                        fixture_id=manifest.fixture_id,
                        fixture_digest=_strip_sha_prefix(manifest.fixture_digest),
                        manifest_sha256=validated.manifest_sha256,
                        input_sha256=dict(validated.input_sha256),
                    )
                )
                bootstrap_checkpoint_id = f"bootstrap-{invocation_id}"
                superstep_id = f"import-{invocation_id}"
                txn.append_strict(
                    SuperstepPlannedEvent(
                        type="superstep_planned",
                        invocation_id=invocation_id,
                        checkpoint_ns=checkpoint_ns,
                        superstep_id=superstep_id,
                        checkpoint_id=bootstrap_checkpoint_id,
                        task_ids=sorted(imported_task_ids),
                    )
                )
                txn.append_strict(
                    SuperstepCommittedEvent(
                        type="superstep_committed",
                        invocation_id=invocation_id,
                        checkpoint_ns=checkpoint_ns,
                        superstep_id=superstep_id,
                        checkpoint_id=canonical_digest(
                            {
                                "superstep_id": superstep_id,
                                "parent_checkpoint_id": bootstrap_checkpoint_id,
                                "write_set_ids": [],
                                "target_tree_id": root_tree_id,
                                "committed_task_ids": sorted(imported_task_ids),
                            }
                        ),
                        parent_checkpoint_id=bootstrap_checkpoint_id,
                        write_set_ids=[],
                        target_tree_id=root_tree_id,
                        state_values={},
                        committed_task_ids=sorted(imported_task_ids),
                    )
                )
        except CheckpointImportError:
            raise
        except Exception as exc:
            if "duplicate" in str(exc).lower():
                raise GraphRuntimeError(f"duplicate invocation start: {invocation_id}") from exc
            raise

        projection = self._checkpoints.project(invocation_id)
        checkpoint_id = f"bootstrap-{invocation_id}"
        # 首个 checkpoint 来自严格 ledger 投影（无 superstep_committed 时用 bootstrap 名）。
        self._checkpoints.write(projection)

        # 导入后从未完成 task 继续；失败不回滚已提交的 import 事件。
        self._drive(invocation_id, bound)
        return ImportResult(
            invocation_id=invocation_id,
            checkpoint_id=checkpoint_id,
            imported_tasks=tuple(imported_task_ids),
        )

    def latest_root_invocation(self, entrypoint: str | None = None) -> str | None:
        return self._checkpoints.latest_root_invocation(entrypoint)

    def invocation_terminal(self, invocation_id: str) -> str | None:
        """Return the terminal status of an invocation, or None if still active."""
        proj = self._try_project(invocation_id)
        return proj.terminal if proj is not None else None

    def run_child(
        self,
        parent_task: ExecutableTask,
        graph_id: str,
        workspace: TaskWorkspace,
        context: RuntimeContext,
    ) -> TaskResult:
        """在父 task workspace 上启动/恢复 named subgraph；完成时冻结 write-set。"""
        compiled = self._schema_resolver_for_parent(parent_task)
        if graph_id not in compiled.graphs:
            return TaskResult(
                status="failed",
                error_kind="contract",
                error=f"unknown subgraph '{graph_id}'",
            )
        child_invocation_id = derive_child_invocation_id(parent_task, graph_id)
        checkpoint_ns = f"{parent_task.checkpoint_ns}/{parent_task.node_id}/{child_invocation_id}"
        structural_path = f"{parent_task.structural_path}/{parent_task.node_id}/{graph_id}"
        child_params = dict(context.params)
        if isinstance(parent_task.input, Mapping):
            bound = parent_task.input.get("with")
            if isinstance(bound, Mapping):
                child_params.update({str(key): value for key, value in bound.items()})
        child_context = context.model_copy(
            update={
                "project_root": workspace.project_root,
                "repo_root": workspace.repo_root,
                "params": child_params,
            }
        )
        existing = self._try_project(child_invocation_id)
        parent_projection = self._checkpoints.project(parent_task.invocation_id)
        if existing is None:
            # 父 task workspace 已物化；child 继承同一 base tree，避免以 workspace
            # project_root 调用 TreeStore.capture（change_dir 在 workspace 外）。
            root_tree_id = workspace.base_tree_id
            try:
                binding = inherit_child_definitions(
                    parent=parent_projection,
                    change_dir=context.change_dir,
                )
            except PolicyError as exc:
                raise GraphRuntimeError(f"policy snapshot: {exc}") from exc
            started = _build_invocation_started(
                invocation_id=child_invocation_id,
                entrypoint=graph_id,
                graph_id=graph_id,
                compiled=compiled,
                params=dict(child_context.params),
                root_tree_id=root_tree_id,
                max_parallel_tasks=compiled.schema.policies.scheduler.max_parallel_tasks,
                checkpoint_ns=checkpoint_ns,
                structural_path=structural_path,
                binding=binding,
                parent_invocation_id=parent_task.invocation_id,
                parent_task_id=parent_task.task_id,
            )
            with transaction(context.change_dir) as txn:
                txn.append_strict(started)
                self._stage_pinned_definitions(txn, compiled, binding)
                txn.write_runtime_file(
                    f".graph-runtime/invocations/{child_invocation_id}.json",
                    json.dumps(
                        {
                            "project_root": str(workspace.project_root),
                            "repo_root": str(workspace.repo_root),
                            "change_id": context.change_id,
                            "parent_session_id": context.parent_session_id,
                            "parent_invocation_id": parent_task.invocation_id,
                            "parent_task_id": parent_task.task_id,
                        },
                        sort_keys=True,
                    ).encode("utf-8"),
                )
        else:
            try:
                binding = inherit_child_definitions(
                    parent=parent_projection,
                    change_dir=context.change_dir,
                )
                verify_pinned_definitions(binding, context.change_dir)
            except PolicyError as exc:
                raise GraphRuntimeError(f"policy snapshot: {exc}") from exc
        result = self._drive(child_invocation_id, child_context)
        return self._child_result_to_task_result(result, parent_task=parent_task, workspace=workspace)

    def _schema_resolver_for_parent(self, parent_task: ExecutableTask) -> CompiledWorkflow:
        parent = self._checkpoints.project(parent_task.invocation_id)
        return self._resolve_compiled(parent)

    def _try_project(self, invocation_id: str) -> GraphProjection | None:
        try:
            return self._checkpoints.project(invocation_id)
        except LedgerIntegrityError:
            return None

    def _child_result_to_task_result(
        self,
        result: RunResult,
        *,
        parent_task: ExecutableTask,
        workspace: TaskWorkspace,
    ) -> TaskResult:
        if result.status.status == "interrupted":
            pending = result.status.pending_interrupts
            interrupt = pending[0] if pending else None
            if interrupt is not None:
                # 父 ledger 上的 bubbled interrupt 锚定父 subgraph node，保留 child ns。
                interrupt = interrupt.model_copy(update={"node_id": parent_task.node_id})
            return TaskResult(status="interrupted", interrupt=interrupt)
        if result.status.status == "stopped":
            return TaskResult(status="stopped", error=result.reason)
        if result.status.status == "failed":
            return TaskResult(
                status="failed",
                error_kind="internal",
                error=result.reason,
            )
        if result.status.status != "completed":
            return TaskResult(
                status="failed",
                error_kind="internal",
                error=f"child subgraph ended in unexpected status {result.status.status}",
            )
        try:
            write_set = self._objects.freeze_write_set(
                workspace,
                claims=parent_task.resources,
                outputs=_task_outputs(parent_task),
            )
        except WorkspaceError as exc:
            return TaskResult(status="failed", error_kind="invalid_output", error=str(exc))
        return TaskResult(
            status="succeeded",
            write_set_id=write_set.write_set_id,
            outputs_sha256=dict(write_set.outputs_sha256),
        )

    # ------------------------------------------------------------------ start

    def _start_invocation(
        self,
        compiled: CompiledWorkflow,
        entrypoint: str,
        context: RuntimeContext,
    ) -> str:
        # --- Entrypoint restart policy safety net ---
        # For "once" entrypoints, refuse if this entrypoint has a completed
        # invocation.  loop.py enforces this earlier; this is a secondary guard
        # for callers that invoke run() directly without going through the driver.
        ep = compiled.entrypoints.get(entrypoint)
        if ep is not None and ep.restart == "once":
            scoped_latest = self.latest_root_invocation(entrypoint)
            if scoped_latest is not None:
                try:
                    scoped_proj = self._checkpoints.project(scoped_latest)
                except LedgerIntegrityError as exc:
                    raise GraphIntegrityError(str(exc)) from exc
                if scoped_proj.terminal is not None:
                    raise GraphRuntimeError(
                        f"entrypoint '{entrypoint}' already completed "
                        f"(invocation {scoped_latest}); refuse restart"
                    )

        # --- Active invocation guard (any entrypoint) ---
        latest = self.latest_root_invocation()
        if latest is not None:
            try:
                existing = self._checkpoints.project(latest)
            except LedgerIntegrityError as exc:
                raise GraphIntegrityError(str(exc)) from exc
            if existing.terminal is None:
                raise GraphRuntimeError(
                    f"change already has active invocation {latest}; resume instead of run"
                )

        if entrypoint not in compiled.entrypoints:
            raise GraphRuntimeError(f"unknown entrypoint '{entrypoint}'")
        entry = compiled.entrypoints[entrypoint]
        try:
            params = resolve_params(compiled.schema, {**entry.param_overrides, **context.params})
        except Exception as exc:  # CompileError
            raise GraphRuntimeError(f"invalid params: {exc}") from exc
        if entry.allow_expr is not None and not is_satisfied(entry.allow_expr, Scope({"params": params})):
            raise GraphRuntimeError(f"entrypoint '{entrypoint}' allow expression rejected params")

        if entrypoint == "retro":
            try:
                params = ensure_retro_params(params)
            except Exception as exc:
                raise GraphRuntimeError(f"invalid retro params: {exc}") from exc

        root_tree_id = self._objects.capture(context.project_root, repo_root=context.repo_root)
        invocation_id = str(uuid4())
        checkpoint_ns = invocation_id
        canonical_digest(params)
        max_parallel = compiled.schema.policies.scheduler.max_parallel_tasks
        graph_id = entry.graph_id

        try:
            binding = bind_root_definitions(store=self._objects, root_tree_id=root_tree_id)
        except PolicyError:
            raise
        started = _build_invocation_started(
            invocation_id=invocation_id,
            entrypoint=entrypoint,
            graph_id=graph_id,
            compiled=compiled,
            params=params,
            root_tree_id=root_tree_id,
            max_parallel_tasks=max_parallel,
            checkpoint_ns=checkpoint_ns,
            structural_path=graph_id,
            binding=binding,
        )
        try:
            with transaction(context.change_dir) as txn:
                txn.append_strict(started)
                self._stage_pinned_definitions(txn, compiled, binding)
                txn.write_runtime_file(
                    f".graph-runtime/invocations/{invocation_id}.json",
                    json.dumps(
                        {
                            "project_root": str(context.project_root),
                            "repo_root": str(context.repo_root),
                            "change_id": context.change_id,
                            "parent_session_id": context.parent_session_id,
                        },
                        sort_keys=True,
                    ).encode("utf-8"),
                )
        except Exception as exc:
            if "duplicate" in str(exc).lower():
                raise GraphRuntimeError(f"duplicate invocation start: {invocation_id}") from exc
            raise
        return invocation_id

    def _stage_pinned_definitions(
        self,
        txn: object,
        compiled: CompiledWorkflow,
        binding: InvocationDefinitionBinding,
    ) -> None:
        stage_pinned_definitions(
            txn,  # type: ignore[arg-type]
            compiled,
            binding,
            contracts=self._contracts,
        )

    # ------------------------------------------------------------------ resume

    def _recover_and_drive(
        self,
        invocation_id: str,
        command: ResumeCommand | None,
    ) -> RunResult:
        try:
            projection = self._checkpoints.project(invocation_id)
        except LedgerIntegrityError as exc:
            raise GraphIntegrityError(str(exc)) from exc
        context = self._context_for(projection)
        if projection.terminal is not None:
            status = graph_status_from_projection(
                projection,
                pending_write_sets=self._pending_write_sets(invocation_id),
            )
            return RunResult(
                invocation_id=invocation_id,
                status=status,
                exit_code=_exit_for_status(status.status),
                reason=projection.terminal_reason or status.status,
            )
        if command is not None:
            self._commit_resume_command(projection, context, command)
            if command.action == "stop":
                status = self.status(invocation_id)
                return RunResult(
                    invocation_id=invocation_id,
                    status=status,
                    exit_code=EXIT_STOPPED,
                    reason=command.reason,
                )
        return self._drive(invocation_id, context)

    def _commit_resume_command(
        self,
        projection: GraphProjection,
        context: RuntimeContext,
        command: ResumeCommand,
    ) -> None:
        if not command.reason.strip() or not command.who.strip():
            raise GraphRuntimeError("resume requires nonblank reason and who")
        pending = projection.interrupts.get(command.interrupt_id)
        if pending is None or pending.resolved_action is not None:
            raise GraphRuntimeError(f"interrupt {command.interrupt_id} is not pending")
        if command.action not in pending.actions:
            raise GraphRuntimeError(
                f"action {command.action!r} not allowed for interrupt {command.interrupt_id}"
            )
        audited = self._rehash_artifact_view(context.change_dir, pending)
        # Emit graph_resumed for every invocation along the interrupt ns
        # (root → mid → leaf). Writing only root+leaf leaves intermediate
        # subgraphs with a still-pending interrupt, so they re-bubble the
        # same gate instead of honouring resume.action.
        resume_invocation_ids = _invocation_ids_along_ns(pending.checkpoint_ns)
        if projection.invocation_id not in resume_invocation_ids:
            resume_invocation_ids.insert(0, projection.invocation_id)
        with transaction(context.change_dir) as txn:
            if projection.event_schema_version >= 3:
                parent_anchor_ref: str | None = None
                for index, invocation_id in enumerate(resume_invocation_ids):
                    layer_ns = _checkpoint_ns_for_invocation(pending.checkpoint_ns, invocation_id)
                    layer_node = _node_id_for_invocation(
                        pending.checkpoint_ns, invocation_id, pending.node_id
                    )
                    anchor = ResumeAnchor(
                        invocation_id=invocation_id,
                        checkpoint_ns=layer_ns,
                        node_id=layer_node,
                        interrupt_id=command.interrupt_id,
                    )
                    txn.append_strict(
                        GraphResumedEvent(
                            type="graph_resumed",
                            invocation_id=invocation_id,
                            checkpoint_ns=layer_ns,
                            interrupt_id=command.interrupt_id,
                            action=command.action,
                            reason=command.reason,
                            who=command.who,
                            audited_reads_sha256=audited if index == 0 else {},
                            anchor=anchor,
                            parent_anchor_ref=parent_anchor_ref,
                            payload=command.payload if index == 0 else {},
                        )
                    )
                    parent_anchor_ref = canonical_digest(anchor.model_dump(mode="json"))
            else:
                resumed = GraphResumedEvent(
                    type="graph_resumed",
                    invocation_id=resume_invocation_ids[0],
                    checkpoint_ns=pending.checkpoint_ns,
                    interrupt_id=command.interrupt_id,
                    action=command.action,
                    reason=command.reason,
                    who=command.who,
                    audited_reads_sha256=audited,
                    payload=command.payload,
                )
                for index, invocation_id in enumerate(resume_invocation_ids):
                    if index == 0:
                        txn.append_strict(resumed)
                    else:
                        txn.append_strict(
                            resumed.model_copy(update={"invocation_id": invocation_id, "payload": {}})
                        )
            if command.action == "stop":
                txn.append_strict(
                    GraphTerminalEvent(
                        type="graph_stopped",
                        invocation_id=projection.invocation_id,
                        checkpoint_ns=projection.checkpoint_ns,
                        reason=command.reason,
                    )
                )
                live = fold_after_append(
                    context.change_dir,
                    projection.invocation_id,
                    "graph_stopped",
                    command.reason,
                    projection,
                )
                txn.set_workflow_state_projection(render_workflow_state_yaml(live))

    def _rehash_artifact_view(
        self,
        change_dir: Path,
        pending: InterruptProjection,
    ) -> dict[str, str]:
        if not pending.audited_reads_sha256:
            return {}
        if not pending.artifact_view:
            raise GraphRuntimeError(
                f"interrupt {pending.interrupt_id} lacks artifact_view for audited resume"
            )
        view_root = change_dir / pending.artifact_view
        audited: dict[str, str] = {}
        for rel, expected in sorted(pending.audited_reads_sha256.items()):
            path = view_root / rel
            actual = sha256_file(path)
            if actual is None or actual != expected:
                raise GraphRuntimeError(f"audited read drift for interrupt {pending.interrupt_id}: {rel}")
            audited[rel] = actual
        return audited

    def _context_for(self, projection: GraphProjection) -> RuntimeContext:
        change_dir = self._checkpoints._change_dir  # noqa: SLF001
        meta_path = change_dir / ".graph-runtime" / "invocations" / f"{projection.invocation_id}.json"
        project_root = change_dir.parent.parent.parent
        repo_root = project_root
        change_id = change_dir.name
        parent_session_id: str | None = None
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            project_root = Path(meta["project_root"])
            repo_root = Path(meta.get("repo_root", project_root))
            change_id = str(meta.get("change_id", change_id))
            parent_session_id = meta.get("parent_session_id")
        except (OSError, KeyError, TypeError, json.JSONDecodeError):
            pass
        return RuntimeContext(
            project_root=project_root,
            repo_root=repo_root,
            change_dir=change_dir,
            change_id=change_id,
            params=dict(projection.params),
            parent_session_id=parent_session_id,
        )

    def _child_projections(self, invocation_id: str) -> dict[str, GraphProjection]:
        projections: dict[str, GraphProjection] = {}
        for raw in read_events_strict(self._checkpoints._change_dir):  # noqa: SLF001
            if raw.get("source") != "graph" or raw.get("type") != "graph_invocation_started":
                continue
            if raw.get("parent_invocation_id") != invocation_id:
                continue
            child_id = raw.get("invocation_id")
            if not isinstance(child_id, str):
                continue
            try:
                projections[child_id] = self._checkpoints.project(child_id)
            except LedgerIntegrityError:
                continue
        return projections

    # ------------------------------------------------------------------ drive

    def _drive(self, invocation_id: str, context: RuntimeContext) -> RunResult:
        artifacts = self._objects
        while True:
            try:
                projection = self._reach_recovery_barrier(invocation_id, context)
            except LedgerIntegrityError as exc:
                raise GraphIntegrityError(str(exc)) from exc

            if projection.terminal is not None:
                return self._result_from_projection(projection)

            compiled = self._resolve_compiled(projection)

            wait_until = self._earliest_retry_at(projection)
            if wait_until is not None and wait_until > self._clock.now():
                delay = (wait_until - self._clock.now()).total_seconds()
                if delay > 0:
                    self._clock.sleep(delay)
                continue

            try:
                plan = plan_superstep(compiled, projection, context, artifacts)  # type: ignore[arg-type]
            except PlanError as exc:
                message = str(exc)
                if exc.error_kind == "graph_definition_changed":
                    raise GraphDefinitionChanged(message) from exc
                raise GraphRuntimeError(message) from exc

            child_projections = self._child_projections(invocation_id)
            inherited_lease = context.inherited_prepared_wave_lease(
                self._scheduler._prepared_wave_lease_owner  # noqa: SLF001
            )
            from assurance_agent.workflow.graph.selected_wave import PreparedWaveLease

            typed_lease = inherited_lease if isinstance(inherited_lease, PreparedWaveLease) else None
            prepared_entry = typed_lease.for_invocation(invocation_id) if typed_lease is not None else None
            selected = preview_selected_wave(
                compiled,
                projection,
                context,
                artifacts,  # type: ignore[arg-type]
                max_parallel_tasks=compiled.schema.policies.scheduler.max_parallel_tasks,
                child_projections=child_projections,
            )
            uses_prepared = prepared_entry is not None or (
                selected is not None and (selected.synchronized_paths or selected.lock_tokens)
            )
            if plan.strict_events and not uses_prepared:
                with transaction(context.change_dir) as txn:
                    for event in plan.strict_events:
                        txn.append_strict(event)

            if plan.terminal is not None:
                return self._finish_terminal(invocation_id, context, plan)

            if not plan.tasks:
                if plan.strict_events:
                    if uses_prepared:
                        with transaction(context.change_dir) as txn:
                            for event in plan.strict_events:
                                txn.append_strict(event)
                    continue
                raise GraphRuntimeError("planner returned no tasks and no terminal")

            try:
                if prepared_entry is not None:
                    assert typed_lease is not None
                    wave = self._scheduler.execute_selected_wave(
                        typed_lease,
                        context,
                        invocation_id=invocation_id,
                        compiled=compiled,
                        artifacts=artifacts,  # type: ignore[arg-type]
                        child_projections=child_projections,
                    )
                else:
                    wave = self._scheduler.execute(
                        plan,
                        projection,
                        context,
                        selected_wave=selected if uses_prepared else None,
                        compiled=compiled if uses_prepared else None,
                        artifacts=artifacts if uses_prepared else None,  # type: ignore[arg-type]
                        child_projections=child_projections if uses_prepared else None,
                        inherited_lease=typed_lease,
                    )
            except SelectedWaveDriftError as exc:
                raise GraphRuntimeError(str(exc)) from exc
            except (SchedulerError, ProgressionError, WorkspaceError) as exc:
                raise GraphRuntimeError(str(exc)) from exc

            projection = self._checkpoints.project(invocation_id)
            if projection.terminal is not None:
                return self._result_from_projection(projection)

            pending_interrupts = [i for i in projection.interrupts.values() if i.resolved_action is None]
            if pending_interrupts and not plan.tasks:
                return self._result_from_projection(projection)

            if wave.interrupted:
                projection = self._checkpoints.project(invocation_id)
                return self._result_from_projection(projection)

            if wave.stopped:
                reason = "task stopped"
                for task_id in wave.stopped:
                    task_proj = projection.tasks.get(task_id)
                    if task_proj is not None and isinstance(task_proj.value, dict):
                        raw = task_proj.value.get("reason")
                        if isinstance(raw, str) and raw.strip():
                            reason = raw
                            break
                    elif (
                        task_proj is not None and isinstance(task_proj.value, str) and task_proj.value.strip()
                    ):
                        reason = task_proj.value
                        break
                stop_plan = PlanResult(
                    superstep_id=plan.superstep_id,
                    checkpoint_id=plan.checkpoint_id,
                    tasks=(),
                    terminal="stop",
                    reason=reason,
                )
                return self._finish_terminal(invocation_id, context, stop_plan)

            if wave.retry_at is not None:
                retry_at = _parse_ts(wave.retry_at)
                delay = (retry_at - self._clock.now()).total_seconds()
                if delay > 0:
                    self._clock.sleep(delay)
                continue

            continue

    def _finish_terminal(
        self,
        invocation_id: str,
        context: RuntimeContext,
        plan: PlanResult,
    ) -> RunResult:
        assert plan.terminal is not None
        if plan.terminal == "interrupt":
            projection = self._checkpoints.project(invocation_id)
            return self._result_from_projection(projection)

        projection = self._checkpoints.project(invocation_id)
        if self._recovery_work_remains(projection, invocation_id, context):
            self._reach_recovery_barrier(invocation_id, context)
            projection = self._checkpoints.project(invocation_id)
            if self._recovery_work_remains(projection, invocation_id, context):
                raise GraphRuntimeError("cannot terminal while durable recovery work remains")

        event_type: Literal["graph_completed", "graph_stopped", "graph_failed"]
        if plan.terminal == "end":
            event_type = "graph_completed"
        elif plan.terminal == "stop":
            event_type = "graph_stopped"
        else:
            event_type = "graph_failed"
        reason = plan.reason or plan.terminal
        with transaction(context.change_dir) as txn:
            txn.append_strict(
                GraphTerminalEvent(
                    type=event_type,
                    invocation_id=invocation_id,
                    checkpoint_ns=projection.checkpoint_ns,
                    reason=reason,
                )
            )
            live = fold_after_append(context.change_dir, invocation_id, event_type, reason, projection)
            txn.set_workflow_state_projection(render_workflow_state_yaml(live))
        return self._result_from_projection(self._checkpoints.project(invocation_id))

    def _result_from_projection(self, projection: GraphProjection) -> RunResult:
        status = graph_status_from_projection(
            projection,
            pending_write_sets=self._pending_write_sets(projection.invocation_id),
        )
        return RunResult(
            invocation_id=projection.invocation_id,
            status=status,
            exit_code=_exit_for_status(status.status),
            reason=status.terminal_reason or status.status,
        )

    def _resolve_compiled(self, projection: GraphProjection) -> CompiledWorkflow:
        try:
            compiled = self._schema_resolver(projection.graph_digest)
        except Exception as exc:
            raise GraphDefinitionChanged(
                f"schema_resolver failed for digest {projection.graph_digest}: {exc}"
            ) from exc
        if compiled.digest != projection.graph_digest:
            raise GraphDefinitionChanged(
                f"graph_definition_changed: resolver digest {compiled.digest} != "
                f"pinned {projection.graph_digest}"
            )
        if compiled.contract_digests != projection.contract_digests:
            raise GraphDefinitionChanged("graph_definition_changed: contract digests drifted")
        return compiled

    def _reconcile_running(self, projection: GraphProjection, context: RuntimeContext) -> None:
        leases = LeaseRegistry(context.change_dir)
        recover_running_tasks(
            context.change_dir,
            projection=projection,
            leases=leases.read_all(),
            reconnect_for=lambda _task_id: False,
            probe=SystemLivenessProbe(),
            now=self._clock.now(),
        )

    def _reach_recovery_barrier(self, invocation_id: str, context: RuntimeContext) -> GraphProjection:
        """Reconcile and replay durable updates until no recovery seam reports progress."""
        while True:
            projection = self._checkpoints.project(invocation_id)
            self._reconcile_running(projection, context)
            projection = self._checkpoints.project(invocation_id)

            progress = False
            if self._commit_pending_write_sets(projection, context):
                progress = True
                projection = self._checkpoints.project(invocation_id)
            if self._replay_committed_publications(projection, context):
                progress = True
                projection = self._checkpoints.project(invocation_id)

            if not progress and not self._recovery_work_remains(projection, invocation_id, context):
                break
            if not progress:
                raise GraphRuntimeError("recovery barrier stalled with durable work remaining")

        projection = self._checkpoints.project(invocation_id)
        self._repair_ordinary_materialization(projection, context)
        return self._checkpoints.project(invocation_id)

    def _recovery_work_remains(
        self,
        projection: GraphProjection,
        invocation_id: str,
        context: RuntimeContext,
    ) -> bool:
        if any(task.status == "running" for task in projection.tasks.values()):
            return True
        if self._pending_write_sets(invocation_id):
            return True
        planned = self._last_uncommitted_plan(invocation_id)
        if planned is not None and any(
            task.status == "succeeded" and not task.outputs_committed for task in projection.tasks.values()
        ):
            return True
        publication_store = ProjectPublicationStore(context.project_root)
        for publication, status in publication_store.list_publications(invocation_id=invocation_id):
            if status != "applied":
                return True
        return False

    def _commit_pending_write_sets(self, projection: GraphProjection, context: RuntimeContext) -> bool:
        planned = self._last_uncommitted_plan(projection.invocation_id)
        if planned is None:
            return False
        succeeded = [
            task_id
            for task_id, task in projection.tasks.items()
            if task.status == "succeeded" and not task.outputs_committed
        ]
        if not succeeded:
            return False
        plan = PlanResult(
            superstep_id=planned["superstep_id"],
            checkpoint_id=planned["checkpoint_id"],
            tasks=(),
        )
        try:
            return self._scheduler.commit_pending_write_sets(
                plan=plan,
                projection=projection,
                context=context,
                succeeded_ids=succeeded,
            )
        except (WorkspaceError, ProgressionError, SchedulerError, ValueError) as exc:
            raise GraphRuntimeError(f"failed to commit pending write sets: {exc}") from exc

    def _replay_committed_publications(
        self,
        projection: GraphProjection,
        context: RuntimeContext,
    ) -> bool:
        progress = False
        for raw in read_events_strict(self._checkpoints._change_dir):  # noqa: SLF001
            if raw.get("source") != "graph" or raw.get("invocation_id") != projection.invocation_id:
                continue
            if raw.get("type") != "superstep_committed":
                continue
            raw_checkpoint_id = raw.get("checkpoint_id")
            publication_id = raw_checkpoint_id if isinstance(raw_checkpoint_id, str) else None
            raw_ids = raw.get("write_set_ids")
            write_set_ids = tuple(
                value for value in (raw_ids if isinstance(raw_ids, list) else []) if isinstance(value, str)
            )
            if publication_id is None or not write_set_ids:
                continue
            try:
                if self._scheduler.repair_committed_write_sets(
                    context=context,
                    invocation_id=projection.invocation_id,
                    publication_id=publication_id,
                    write_set_ids=write_set_ids,
                ):
                    progress = True
            except (SchedulerError, WorkspaceError) as exc:
                raise GraphRuntimeError(f"failed to replay committed publication: {exc}") from exc
        return progress

    def _repair_ordinary_materialization(
        self,
        projection: GraphProjection,
        context: RuntimeContext,
    ) -> None:
        if not self._ordinary_materialization_drift(projection, context):
            return
        prev, target, _, _write_set_ids = self._last_committed_tree_edge(projection)
        assert target is not None
        base = prev if prev is not None else projection.root_tree_id
        try:
            self._objects.apply_tree(context.project_root, target, base_tree_id=base)
        except WorkspaceError as exc:
            raise GraphRuntimeError(f"failed to repair materialization: {exc}") from exc

    def _ordinary_materialization_drift(
        self,
        projection: GraphProjection,
        context: RuntimeContext,
    ) -> bool:
        prev, target, publication_id, write_set_ids = self._last_committed_tree_edge(projection)
        if target is None or publication_id is None:
            return False
        if write_set_ids:
            write_sets = [self._objects.load_write_set(write_set_id) for write_set_id in write_set_ids]
            if any(write_set.synchronized_paths for write_set in write_sets):
                return False
        try:
            current = self._objects.capture(context.project_root, repo_root=context.repo_root)
        except WorkspaceError:
            return False
        return current != target

    def _last_committed_tree_edge(
        self,
        projection: GraphProjection,
    ) -> tuple[str | None, str | None, str | None, tuple[str, ...]]:
        cursor = projection.root_tree_id
        last_prev: str | None = None
        last_target: str | None = None
        last_publication_id: str | None = None
        last_write_set_ids: tuple[str, ...] = ()
        for raw in read_events_strict(self._checkpoints._change_dir):  # noqa: SLF001
            if raw.get("source") != "graph" or raw.get("invocation_id") != projection.invocation_id:
                continue
            if raw.get("type") != "superstep_committed":
                continue
            target_tree = raw.get("target_tree_id")
            if isinstance(target_tree, str):
                last_prev = cursor
                last_target = target_tree
                raw_checkpoint_id = raw.get("checkpoint_id")
                last_publication_id = raw_checkpoint_id if isinstance(raw_checkpoint_id, str) else None
                raw_ids = raw.get("write_set_ids")
                last_write_set_ids = tuple(
                    value
                    for value in (raw_ids if isinstance(raw_ids, list) else [])
                    if isinstance(value, str)
                )
                cursor = target_tree
        return last_prev, last_target, last_publication_id, last_write_set_ids

    def _last_uncommitted_plan(self, invocation_id: str) -> dict[str, str] | None:
        last_plan: dict[str, str] | None = None
        committed: set[str] = set()
        for raw in read_events_strict(self._checkpoints._change_dir):  # noqa: SLF001
            if raw.get("source") != "graph" or raw.get("invocation_id") != invocation_id:
                continue
            if raw.get("type") == "superstep_planned":
                sid = raw.get("superstep_id")
                cid = raw.get("checkpoint_id")
                if isinstance(sid, str) and isinstance(cid, str):
                    last_plan = {"superstep_id": sid, "checkpoint_id": cid}
            elif raw.get("type") == "superstep_committed":
                sid = raw.get("superstep_id")
                if isinstance(sid, str):
                    committed.add(sid)
        if last_plan is None or last_plan["superstep_id"] in committed:
            return None
        return last_plan

    def _pending_write_sets(self, invocation_id: str) -> tuple[str, ...]:
        return _pending_write_sets_fn(read_events_strict(self._checkpoints.change_dir), invocation_id)

    @staticmethod
    def _earliest_retry_at(projection: GraphProjection) -> datetime | None:
        times = [
            _parse_ts(task.next_retry_at)
            for task in projection.tasks.values()
            if task.next_retry_at is not None and task.status == "failed"
        ]
        return min(times) if times else None


def fold_after_append(
    change_dir: Path,
    invocation_id: str,
    event_type: str,
    reason: str,
    projection: GraphProjection,
) -> GraphProjection:
    """事务内尚未落盘时，构造带 terminal 的投影供 workflow-state staging。"""
    del change_dir, invocation_id  # 签名保留与 Task 11 一致；投影由调用方传入
    terminal_map = {
        "graph_completed": "completed",
        "graph_stopped": "stopped",
        "graph_failed": "failed",
    }
    return projection.model_copy(
        update={
            "terminal": terminal_map[event_type],
            "terminal_reason": reason,
            "event_seq": projection.event_seq + 1,
        }
    )


def _exit_for_status(
    status: Literal["running", "interrupted", "completed", "stopped", "failed"],
) -> Literal[0, 20, 30, 40]:
    if status == "completed":
        return EXIT_COMPLETED  # type: ignore[return-value]
    if status == "stopped":
        return EXIT_STOPPED  # type: ignore[return-value]
    if status == "interrupted":
        return EXIT_HUMAN_REVIEW  # type: ignore[return-value]
    if status == "failed":
        return EXIT_ERROR  # type: ignore[return-value]
    return EXIT_ERROR  # type: ignore[return-value]


def _parse_ts(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _task_outputs(task: ExecutableTask) -> tuple[str, ...]:
    payload = task.input
    if isinstance(payload, dict):
        outputs = payload.get("outputs")
        if isinstance(outputs, list) and all(isinstance(item, str) for item in outputs):
            return tuple(outputs)
    return ()


def _child_invocation_from_ns(checkpoint_ns: str, root_ns: str) -> str | None:
    """``<parent-ns>/<node>/<child-invocation-id>`` → child invocation id。"""
    if checkpoint_ns == root_ns or not checkpoint_ns.startswith(root_ns + "/"):
        return None
    parts = checkpoint_ns.split("/")
    if len(parts) < 3:
        return None
    return parts[-1]


def _invocation_ids_along_ns(checkpoint_ns: str) -> list[str]:
    """Return invocation ids embedded in a checkpoint namespace.

    Namespace shape is ``inv0/node1/inv1/node2/inv2/...`` — even-index segments
    are invocation ids, odd-index segments are node ids.
    """
    parts = [part for part in checkpoint_ns.split("/") if part]
    return [parts[index] for index in range(0, len(parts), 2)]


def _checkpoint_ns_for_invocation(full_ns: str, invocation_id: str) -> str:
    parts = [part for part in full_ns.split("/") if part]
    for index in range(0, len(parts), 2):
        if parts[index] == invocation_id:
            return "/".join(parts[: index + 1])
    return parts[0] if parts else full_ns


def _node_id_for_invocation(full_ns: str, invocation_id: str, default_node_id: str) -> str:
    parts = [part for part in full_ns.split("/") if part]
    for index in range(0, len(parts), 2):
        if parts[index] == invocation_id and index + 1 < len(parts):
            return parts[index + 1]
    return default_node_id


def _strip_sha_prefix(value: str) -> str:
    if value.startswith("sha256:"):
        return value[len("sha256:") :]
    return value


def ensure_retro_params(params: dict[str, object], *, now: datetime | None = None) -> dict[str, object]:
    """Inject a generated retro_id when params.retro_id is empty/missing.

    Called before plan_superstep when entrypoint == "retro" so the retro_id
    path segment is always non-empty and path-safe before any node runs.
    """
    from assurance_agent.identifiers import assert_path_segment_safe

    out = dict(params)
    rid = out.get("retro_id")
    if not isinstance(rid, str) or not rid.strip():
        stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%d-%H%M%S")
        out["retro_id"] = f"retro-{stamp}"
    assert_path_segment_safe(str(out["retro_id"]), label="retro id")
    return out


__all__ = [
    "GraphDefinitionChanged",
    "GraphIntegrityError",
    "GraphRuntime",
    "GraphRuntimeError",
    "ensure_retro_params",
    "graph_status_from_projection",
]
